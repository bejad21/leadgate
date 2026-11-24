"""Agent-loop checks for habits of the model that the prompt rules did not stop: asking first, searching again, truthful
replies and written-out tool calls.
"""
import json

from engine.core.adapter_base import DomainAdapter
from engine.core.agent_loop import (
    ASK_FOR_CONTACT,
    NOTHING_PASSED_ON_REPLY,
    NOTHING_WAS_PASSED_ON,
    SEARCH_AGAIN,
    _with_note,
    answered_from_earlier_results,
    claims_passed_on,
    hold_needs_contact_first,
    run_turn,
)
from engine.core.tools import OptionalNumber, OptionalText, Text, Tool, ToolArgs, WholeNumber
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.test_agent_loop import FakeLLMClient

CAMRY = {"id": 7, "name": "2020 Toyota Camry", "price": 21834.0, "attributes": "{}"}
SEARCH_RESULT = {"matches": [CAMRY], "count": 1}


class SearchArgs(ToolArgs):
    make: OptionalText = None
    price_max: OptionalNumber = None


class ReserveArgs(ToolArgs):
    item_id: WholeNumber
    customer_name: Text
    customer_contact: OptionalText = None


class ShopAdapter(DomainAdapter):
    domain_type = "cars"
    crm_tag = "Cars"

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def tools(self):
        return [Tool("search_inventory", "Search", SearchArgs), Tool("reserve_item", "Hold an item", ReserveArgs)]

    def execute_tool(self, name, args):
        self.executed.append((name, args))
        if name == "reserve_item":
            return {"lead_id": 1, "kind": "reservation", "item_name": CAMRY["name"], "detail": "Held for 24 hours"}
        return SEARCH_RESULT


def says(text):
    return LLMResponse(content=text, tool_calls=[])


def calls(*steps):
    return LLMResponse(content=None, tool_calls=[ToolCall(name=n, arguments=a, id=f"c{i}") for i, (n, a) in enumerate(steps)])


def earlier_search(price=21834.0):
    # two cars were shown, so an answer that lists both is a list answer
    result = {"matches": [{**CAMRY, "price": price}, {**CAMRY, "id": 8, "name": "2019 Toyota Corolla", "price": 15000.0}], "count": 2}
    return [
        {"role": "user", "content": "any Toyota?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "e1", "type": "function", "function": {"name": "search_inventory", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "e1", "name": "search_inventory", "content": json.dumps(result)},
        {"role": "assistant", "content": "Two: the 2020 Toyota Camry at $21,834 and the 2019 Toyota Corolla at $15,000."},
    ]


def test_a_one_off_note_is_a_system_message_unless_it_follows_a_tool_result():
    chat = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert _with_note(chat, "note")[-1] == {"role": "system", "content": "note"}
    after_tool = [*chat, {"role": "tool", "tool_call_id": "c", "name": "search_inventory", "content": "{}"}]
    follow_up = _with_note(after_tool, "note")[-1]
    assert follow_up["role"] == "user" and follow_up["content"].endswith("note") and "not from the customer" in follow_up["content"]
    assert len(chat) == 2  # the chat itself is not changed


# A hold asked for without a contact is not searched for


def test_a_hold_without_a_contact_is_answered_with_no_tool_offered():
    llm = FakeLLMClient([says("Happy to. What phone number or email can we reach you on, Hana?")])
    adapter = ShopAdapter()
    result = run_turn([{"role": "user", "content": "Reserve the Glen Ridge house for me, I'm Hana Kerr"}], adapter, llm)
    assert llm.calls[0]["tools"] == [] and llm.calls[0]["messages"][-1] == {"role": "system", "content": ASK_FOR_CONTACT}
    assert adapter.executed == [] and result.reply.startswith("Happy to")


def test_the_nudge_is_not_kept_in_the_conversation():
    llm = FakeLLMClient([says("What is your phone number or email?")])
    history = [{"role": "user", "content": "Please put a hold on the Camry, I'm Dana Reyes"}]
    run_turn(history, ShopAdapter(), llm)
    assert all(m["content"] != ASK_FOR_CONTACT for m in history)


def test_a_hold_with_a_contact_is_handled_as_before():
    llm = FakeLLMClient([says("ok")])
    run_turn([{"role": "user", "content": "Please hold the Camry. Felix Grant, felix.grant@example.com"}], ShopAdapter(), llm)
    assert llm.calls[0]["tools"] != []


def test_a_contact_given_earlier_in_the_chat_counts():
    history = [{"role": "user", "content": "I'm Mia, mia@example.com"}, {"role": "assistant", "content": "Hi Mia"}, {"role": "user", "content": "keep it aside for me"}]
    assert not hold_needs_contact_first(history)


def test_asking_about_a_hold_is_not_asking_for_one():
    for text in ("Is the Camry on hold?", "do you hold open days?", "hold on, one sec", "what is your reservation policy?"):
        assert not hold_needs_contact_first([{"role": "user", "content": text}]), text


def test_the_ways_of_asking_for_a_hold_are_found():
    for text in (
        "Reserve the Glen Ridge house for me",
        "Please put a hold on the Mays Landing house",
        "Yes please, keep it aside for me.",
        "can you hold the Camry",
        "I'd like to reserve it",
        "set that one aside",
    ):
        assert hold_needs_contact_first([{"role": "user", "content": text}]), text


# A refinement is searched again, not answered from the earlier list


def test_an_answer_from_the_earlier_list_is_sent_back_to_search_again():
    history = [*earlier_search(), {"role": "user", "content": "which of those are certified?"}]
    llm = FakeLLMClient(
        [says("The Camry at $21,834 and the Corolla at $15,000 are certified."), calls(("search_inventory", {"make": "Toyota"})), says("Searched again: one certified.")]
    )
    adapter = ShopAdapter()
    result = run_turn(history, adapter, llm)
    assert adapter.executed == [("search_inventory", {"make": "Toyota"})]
    assert llm.calls[1]["messages"][-1] == {"role": "system", "content": SEARCH_AGAIN} and llm.calls[1]["tools"] != []
    assert llm.calls[1]["tool_choice"] == "required" and llm.calls[0]["tool_choice"] is None  # the retry must call a tool
    offered = {s["function"]["name"] for s in llm.calls[1]["tools"]}
    assert offered == {"search_inventory"}  # no write tool is offered on the forced retry
    assert result.reply == "Searched again: one certified."


def test_when_the_second_try_still_does_not_search_the_first_answer_stands():
    history = [*earlier_search(), {"role": "user", "content": "which are certified?"}]
    llm = FakeLLMClient([says("The Camry is $21,834 and the Corolla is $15,000."), says("It was $21,834 and $15,000.")])
    result = run_turn(history, ShopAdapter(), llm)
    assert result.reply == "The Camry is $21,834 and the Corolla is $15,000." and len(llm.calls) == 2


def test_a_follow_up_about_one_item_is_answered_from_the_chat_without_a_second_call():
    history = [*earlier_search(), {"role": "user", "content": "how much was the Camry again?"}]
    llm = FakeLLMClient([says("The Camry was $21,834.")])
    result = run_turn(history, ShopAdapter(), llm)
    assert result.reply == "The Camry was $21,834." and len(llm.calls) == 1


def test_a_reply_without_a_shown_price_is_left_alone():
    history = [*earlier_search(), {"role": "user", "content": "thanks!"}]
    llm = FakeLLMClient([says("You're welcome!")])
    run_turn(history, ShopAdapter(), llm)
    assert len(llm.calls) == 1


def test_a_price_is_only_compared_with_prices_a_search_showed():
    history = earlier_search()
    assert answered_from_earlier_results("The Camry is $21,834 and the Corolla is $15,000.", history)
    assert not answered_from_earlier_results("It is $21,834.", history)  # one price is a follow-up, not a list
    assert not answered_from_earlier_results("They are $19,999 and $18,000.", history)
    assert not answered_from_earlier_results("It is $21,834 and $15,000.", [{"role": "user", "content": "hi"}])


# A reply may not say a request was passed on when none was created


def test_claims_of_a_request_passed_on_are_found():
    assert claims_passed_on("I've passed your hold request on to our team.")
    assert claims_passed_on("Your request has been forwarded.")
    assert claims_passed_on("I have sent your details to the team, a person will confirm.")
    assert not claims_passed_on("A person will confirm the hold with you shortly.")
    assert not claims_passed_on("I can pass your request on once I have your phone number.")
    assert not claims_passed_on(NOTHING_PASSED_ON_REPLY)


def hold_history():
    return [*earlier_search(), {"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"}]


def test_a_false_claim_gets_one_more_round_in_which_the_hold_is_made():
    llm = FakeLLMClient(
        [
            says("I've passed your hold request on to our team."),
            calls(("reserve_item", {"item_id": 7, "customer_name": "Felix Grant", "customer_contact": "felix.grant@example.com"})),
            says("Done: a person will confirm the hold with you."),
        ]
    )
    adapter = ShopAdapter()
    result = run_turn(hold_history(), adapter, llm)
    assert [name for name, _ in adapter.executed] == ["reserve_item"]
    assert llm.calls[1]["messages"][-1] == {"role": "system", "content": NOTHING_WAS_PASSED_ON}
    assert "passed on your hold request" in result.reply and "not a confirmation" in result.reply


def test_a_claim_after_searches_used_up_the_rounds_is_caught_and_the_hold_is_made():
    reserve = ("reserve_item", {"item_id": 7, "customer_name": "Felix Grant", "customer_contact": "felix.grant@example.com"})
    llm = FakeLLMClient(
        [
            calls(("search_inventory", {})),
            says("I've passed your hold request on to our team."),
            calls(reserve),
            says("Done: a person will confirm the hold with you."),
        ]
    )
    adapter = ShopAdapter()
    history = [{"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"}]
    result = run_turn(history, adapter, llm)
    assert [name for name, _ in adapter.executed] == ["search_inventory", "reserve_item"]
    assert llm.calls[2]["tool_choice"] == "required" and {s["function"]["name"] for s in llm.calls[2]["tools"]} == {"reserve_item"}
    note = llm.calls[2]["messages"][-1]  # it follows a tool result, so it is a user message that says who it is from
    assert note["role"] == "user" and note["content"].endswith(NOTHING_WAS_PASSED_ON) and "not from the customer" in note["content"]
    assert "passed on your hold request" in result.reply  # built from the tool result, not from the model wording
    assert len(llm.calls) == 3


def test_a_claim_that_stays_false_is_replaced_by_the_plain_truth():
    llm = FakeLLMClient([says("I've passed your hold request on to our team."), says("I've forwarded your request to the team.")])
    adapter = ShopAdapter()
    result = run_turn(hold_history(), adapter, llm)
    assert adapter.executed == [] and result.reply == NOTHING_PASSED_ON_REPLY


def test_a_claim_is_believed_when_a_request_was_made_earlier_in_the_chat():
    done = {"lead_id": 1, "kind": "reservation", "item_name": "2020 Toyota Camry"}
    history = [
        *earlier_search(),
        {"role": "tool", "tool_call_id": "h1", "name": "reserve_item", "content": json.dumps(done)},
        {"role": "user", "content": "did you send my request?"},
    ]
    llm = FakeLLMClient([says("Yes, I've passed your hold request on to our team.")])
    result = run_turn(history, ShopAdapter(), llm)
    assert len(llm.calls) == 1 and result.reply.startswith("Yes")


def test_a_claim_after_a_real_hold_this_turn_is_left_alone():
    llm = FakeLLMClient(
        [
            calls(("reserve_item", {"item_id": 7, "customer_name": "Felix Grant", "customer_contact": "felix.grant@example.com"})),
            says("I've passed your hold request on to our team; a person will confirm."),
        ]
    )
    result = run_turn(hold_history(), ShopAdapter(), llm)
    assert len(llm.calls) == 2 and "passed your hold request" in result.reply


def test_a_hold_a_viewing_and_a_plain_lead_all_count_as_a_request_passed_on():
    from engine.core.agent_loop import _write_succeeded_in, request_passed_on_reply

    write_tools = ShopAdapter.write_tools
    for name, result in (
        ("reserve_item", {"lead_id": 1, "kind": "reservation", "item_name": "Camry"}),
        ("book_viewing", {"lead_id": 2, "kind": "viewing", "item_name": "Camry", "detail": "Sat, morning"}),
        ("create_lead", {"lead_id": 3, "item_name": "Camry"}),
    ):
        history = [{"role": "tool", "tool_call_id": "c", "name": name, "content": json.dumps(result)}]
        assert _write_succeeded_in(history, write_tools), name
        assert request_passed_on_reply([result]) is not None, name
    failed = [{"role": "tool", "tool_call_id": "c", "name": "reserve_item", "content": json.dumps({"error": "too many tool calls in one turn"})}]
    searched = [{"role": "tool", "tool_call_id": "c", "name": "search_inventory", "content": json.dumps(SEARCH_RESULT)}]
    assert not _write_succeeded_in(failed, write_tools) and not _write_succeeded_in(searched, write_tools)


def test_a_failed_hold_is_not_reported_as_passed_on_even_when_the_model_says_so():
    reserve = ("reserve_item", {"item_id": 7, "customer_name": "Felix Grant", "customer_contact": "felix.grant@example.com"})

    class HoldRefused(ShopAdapter):
        def execute_tool(self, name, args):
            self.executed.append((name, args))
            return {"ok": False, "error": "hold_limit_reached"} if name == "reserve_item" else SEARCH_RESULT

    llm = FakeLLMClient(
        [
            calls(("search_inventory", {})),
            calls(reserve),
            says("I've passed your hold request on to our team."),
            says("I've passed your hold request on to our team."),
        ]
    )
    history = [{"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"}]
    result = run_turn(history, HoldRefused(), llm)
    assert result.reply == NOTHING_PASSED_ON_REPLY  # the hold was refused, so no hold exists


def test_three_searches_use_up_the_cap_but_the_hold_the_customer_asked_for_still_goes_through():
    reserve = ("reserve_item", {"item_id": 7, "customer_name": "Felix Grant", "customer_contact": "felix.grant@example.com"})
    llm = FakeLLMClient([calls(("search_inventory", {}), ("search_inventory", {"make": "Toyota"}), ("search_inventory", {"price_max": 30000})), calls(reserve), says("Done.")])
    adapter = ShopAdapter()
    result = run_turn([{"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"}], adapter, llm)
    assert [name for name, _ in adapter.executed] == ["search_inventory"] * 3 + ["reserve_item"]
    assert result.tool_results[3].get("kind") == "reservation"


def test_a_fourth_search_is_still_refused():
    steps = [calls(("search_inventory", {"price_max": n})) for n in (1, 2, 3, 4)]
    llm = FakeLLMClient([*steps, says("Here is what I found.")])
    adapter = ShopAdapter()
    result = run_turn([{"role": "user", "content": "show me cars"}], adapter, llm)
    assert [name for name, _ in adapter.executed] == ["search_inventory"] * 3  # the fourth was not run
    assert all("error" not in r for r in result.tool_results[:3])


def test_a_hold_for_an_item_the_chat_never_showed_sends_the_model_to_search_then_holds():
    reserve = ("reserve_item", {"item_id": 7, "customer_name": "Dana Reyes", "customer_contact": "dana@example.com"})
    llm = FakeLLMClient([calls(reserve), calls(("search_inventory", {"make": "Toyota"})), calls(reserve), says("Done.")])
    adapter = ShopAdapter()
    history = [{"role": "user", "content": "Yes, hold the Camry please. I'm Dana Reyes, dana@example.com"}]  # no search result in it
    result = run_turn(history, adapter, llm)
    assert [name for name, _ in adapter.executed] == ["search_inventory", "reserve_item"]  # the first, unshown hold never ran
    assert result.tool_results[0]["error"].startswith("Nothing was held or booked") and "error" not in result.tool_results[2]
    assert result.reply == "Done."


def test_an_item_that_no_search_finds_is_still_not_held():
    reserve = ("reserve_item", {"item_id": 99, "customer_name": "Dana Reyes", "customer_contact": "dana@example.com"})
    llm = FakeLLMClient([calls(reserve), calls(("search_inventory", {})), calls(reserve), says("I could not find that one.")])
    adapter = ShopAdapter()
    result = run_turn([{"role": "user", "content": "Hold item 99. I'm Dana Reyes, dana@example.com"}], adapter, llm)
    assert [name for name, _ in adapter.executed] == ["search_inventory"]
    assert result.reply == "I could not find that one."


def test_after_a_restart_an_earlier_reply_that_passed_a_request_on_is_believed():
    # only the messages and replies survive a restart, not the tool results behind them
    history = [
        {"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"},
        {"role": "assistant", "content": "Thanks! I've passed on your hold request for the 2020 Camry to our team."},
        {"role": "user", "content": "did you send my request?"},
    ]
    llm = FakeLLMClient([says("Yes, I've passed your hold request on to our team and a person will confirm.")])
    result = run_turn(history, ShopAdapter(), llm)
    assert len(llm.calls) == 1 and result.reply.startswith("Yes")


def test_a_chat_that_never_passed_anything_on_still_gets_the_guard():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello! What are you looking for?"}, {"role": "user", "content": "did you send my request?"}]
    llm = FakeLLMClient([says("Yes, I've passed your request on to our team."), says("Yes, I've forwarded your request to the team.")])
    assert run_turn(history, ShopAdapter(), llm).reply == NOTHING_PASSED_ON_REPLY


# Edge cases of the ask-first and passed-on checks


def test_a_bare_price_or_year_is_not_a_phone_number_for_the_ask_first_check():
    for text in ("reserve it, 1250000 is my budget", "the 2019 one with 40000 km, please hold it", "hold the 2020 one at 21834"):
        assert hold_needs_contact_first([{"role": "user", "content": text}]), text
    assert not hold_needs_contact_first([{"role": "user", "content": "hold it, call me on 050 111 2222"}])
    assert not hold_needs_contact_first([{"role": "user", "content": "hold it, mail me at a.b@example.com"}])


def test_harmless_replies_that_use_the_same_verbs_are_not_claims():
    for text in ("I've sent you the details above.", "I've noted your interest in SUVs.", "I've recorded the details for you.", "Your email has been sent."):
        assert not claims_passed_on(text), text
    for text in ("I've passed your request on to our team.", "I've forwarded your hold request.", "I have sent your details to the team.", "Your request has been forwarded."):
        assert claims_passed_on(text), text


def test_an_old_hold_cannot_vouch_for_a_new_request():
    from engine.core.agent_loop import asks_for_something_new

    done = {"lead_id": 1, "kind": "reservation", "item_name": "2020 Toyota Camry"}
    history = [
        *earlier_search(),
        {"role": "tool", "tool_call_id": "h1", "name": "reserve_item", "content": json.dumps(done)},
        {"role": "assistant", "content": "I've passed your hold request on to our team."},
        {"role": "user", "content": "Now hold the Corolla too please. Dana Reyes, dana@example.com"},
    ]
    assert asks_for_something_new("Now hold the Corolla too please") and not asks_for_something_new("did you send my request?")
    llm = FakeLLMClient([says("I've forwarded your hold request for the Corolla to the team."), says("I've forwarded your hold request to the team.")])
    assert run_turn(history, ShopAdapter(), llm).reply == NOTHING_PASSED_ON_REPLY  # the earlier hold is not this one


def test_a_search_that_found_only_a_held_car_still_says_so_when_the_reply_is_blank():
    class OnlyHeld(ShopAdapter):
        def execute_tool(self, name, args):
            self.executed.append((name, args))
            return {"matches": [], "count": 0, "unavailable": [{"name": "2022 Dodge Challenger", "status": "on hold"}]}

    llm = FakeLLMClient([calls(("search_inventory", {"make": "Dodge"})), says(""), says(""), says("")])
    reply = run_turn([{"role": "user", "content": "any Dodge?"}], OnlyHeld(), llm).reply
    assert "2022 Dodge Challenger is on hold" in reply


def test_a_turn_that_calls_no_tool_does_not_build_the_tool_state():
    class Counting(ShopAdapter):
        built = 0

        def tool_models(self):
            Counting.built += 1
            return super().tool_models()

    run_turn([{"role": "user", "content": "hello"}], Counting(), FakeLLMClient([says("Hi! What are you looking for?")]))
    assert Counting.built == 0


def test_the_forced_hold_is_only_for_a_hold_the_customer_asked_for_with_a_contact_given():
    from engine.core.agent_loop import _forced_hold_schemas

    schemas = ShopAdapter().tool_schemas()
    names = lambda s: {x["function"]["name"] for x in s} if s else None  # noqa: E731
    asked = [{"role": "user", "content": "Hold the Camry please. Felix Grant, felix.grant@example.com"}]
    assert names(_forced_hold_schemas(asked, schemas)) == {"reserve_item"}
    no_contact = [{"role": "user", "content": "Hold the Camry please"}]
    assert _forced_hold_schemas(no_contact, schemas) is None
    not_a_hold = [{"role": "user", "content": "I'll take the Camry. Felix Grant, felix.grant@example.com"}]
    assert _forced_hold_schemas(not_a_hold, schemas) is None  # a lead is left to the model


def test_an_availability_question_after_an_earlier_search_is_answered_from_a_new_search():
    from engine.core.agent_loop import CHECK_AGAIN, asks_if_still_available

    history = [*earlier_search(), {"role": "user", "content": "Is the Camry still available?"}]
    assert asks_if_still_available(history)
    llm = FakeLLMClient([says("Yes, the Camry is still available at $21,834."), calls(("search_inventory", {"make": "Toyota"})), says("The Camry is on hold now.")])
    adapter = ShopAdapter()
    result = run_turn(history, adapter, llm)
    assert adapter.executed == [("search_inventory", {"make": "Toyota"})]
    assert llm.calls[1]["messages"][-1] == {"role": "system", "content": CHECK_AGAIN} and llm.calls[1]["tool_choice"] == "required"
    assert {s["function"]["name"] for s in llm.calls[1]["tools"]} == {"search_inventory"}
    assert result.reply == "The Camry is on hold now."


def test_availability_questions_are_found_and_other_questions_are_not():
    from engine.core.agent_loop import asks_if_still_available

    for text in ("Is the Camry still available?", "is it available", "Has the Corolla been sold?", "is that one sold", "is the 2019 Corolla still for sale?"):
        assert asks_if_still_available([*earlier_search(), {"role": "user", "content": text}]), text
    for text in ("how much was the Camry?", "which ones are certified?", "thanks, that sounds available soon"):
        assert not asks_if_still_available([*earlier_search(), {"role": "user", "content": text}]), text
    assert not asks_if_still_available([{"role": "user", "content": "Is the Camry still available?"}])  # nothing was shown before


def test_an_availability_question_with_no_earlier_search_costs_no_extra_call():
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Toyota"})), says("Yes, one Camry.")])
    adapter = ShopAdapter()
    run_turn([{"role": "user", "content": "Is the Camry available?"}], adapter, llm)
    assert len(llm.calls) == 2  # the model searched on its own; nothing was forced


# A written-out tool call must never reach a customer


def test_a_tool_call_written_out_as_a_reply_is_recognised_and_ordinary_words_are_not():
    from engine.core.agent_loop import looks_like_tool_call_text

    for text in ('hold_item{"item_id": "62"}', 'reserve_item(item_id=62, customer_name="Xavier")', '[TOOL_CALLS] [{"name": "x"}]', "<tool_call>{}</tool_call>", ' create_lead {"name": "Camry"} '):
        assert looks_like_tool_call_text(text), text
    for text in ("Happy to hold it. What is your phone number?", "I found 3 cars (see below).", "", "The price is {approximately} $20,000, okay?"):
        assert not looks_like_tool_call_text(text), text


def test_a_tool_call_written_out_while_asking_for_a_contact_becomes_the_question():
    from engine.core.agent_loop import ASK_FOR_CONTACT_REPLY

    llm = FakeLLMClient([says('hold_item{"item_id": "62"}'), says('hold_item{"item_id": "62"}')])
    result = run_turn([{"role": "user", "content": "Hold the Louisville Forester for me, I'm Xavier Neal."}], ShopAdapter(), llm)
    assert result.reply == ASK_FOR_CONTACT_REPLY and "phone number or email" in result.reply
    assert llm.calls[1]["tools"] == []  # the second try asks for words and offers no tool


def test_a_written_out_tool_call_is_retried_once_for_plain_words():
    llm = FakeLLMClient([says('reserve_item{"item_id": 7}'), says("Which car would you like me to hold?")])
    assert run_turn([{"role": "user", "content": "hello"}], ShopAdapter(), llm).reply == "Which car would you like me to hold?"


def test_a_written_out_tool_call_that_stays_one_is_replaced_in_any_chat():
    from engine.core.agent_loop import SAY_AGAIN_REPLY

    llm = FakeLLMClient([says('reserve_item{"item_id": 7}'), says('reserve_item{"item_id": 7}')])
    assert run_turn([{"role": "user", "content": "hello"}], ShopAdapter(), llm).reply == SAY_AGAIN_REPLY


def test_the_prompt_tells_the_model_how_to_read_units_and_bare_figures():
    from engine.core.agent_loop import SYSTEM_PROMPT

    assert "kilometres" in SYSTEM_PROMPT and "40k tops" in SYSTEM_PROMPT
    assert "Never correct yourself" in SYSTEM_PROMPT


def test_the_prompt_asks_for_name_and_contact_when_someone_wants_an_agent_and_gave_neither():
    from engine.core.agent_loop import SYSTEM_PROMPT

    assert "sign me up with an agent" in SYSTEM_PROMPT
    assert "A search with no filter at all is never useful" in SYSTEM_PROMPT


def test_the_prompt_asks_for_numbered_results_and_explains_what_a_position_means():
    from engine.core.agent_loop import SYSTEM_PROMPT

    assert "numbered list" in SYSTEM_PROMPT and "the third one" in SYSTEM_PROMPT
    assert "that place in the list you showed last" in SYSTEM_PROMPT
    assert "ask which, naming the choices" in SYSTEM_PROMPT  # a question only when it is really unclear


def test_the_prompt_says_to_take_every_detail_given_at_once_and_never_ask_twice():
    from engine.core.agent_loop import SYSTEM_PROMPT

    assert "never ask again for something they already told you" in SYSTEM_PROMPT
    assert "missing in one question" in SYSTEM_PROMPT


def test_the_prompt_sets_a_short_acknowledging_voice_and_a_soft_way_to_ask_about_money():
    from engine.core.agent_loop import SYSTEM_PROMPT

    assert "keep replies short" in SYSTEM_PROMPT and "acknowledging" in SYSTEM_PROMPT
    assert "Do you have a rough budget in mind?" in SYSTEM_PROMPT
    assert "a person can talk that through" in SYSTEM_PROMPT  # an offer or a neighbourhood judgement goes to a person
