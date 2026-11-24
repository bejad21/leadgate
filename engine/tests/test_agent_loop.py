import json

import pytest

from engine.core import guardrails
from engine.core.adapter_base import DomainAdapter
from engine.core.agent_loop import SYSTEM_PROMPT, AgentTurnResult, run_turn
from engine.core.tools import OptionalNumber, OptionalText, Text, Tool, ToolArgs, WholeNumber
from engine.llm_client import INVALID_ARGUMENTS, LLMResponse, ToolCall
from engine.rate_limiter import FixedWindowRateLimiter


class FakeLLMClient:
    """Test double for LLMClient: returns canned responses in sequence and
    records every call's message history so the test can assert on it."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        return self._ask(messages, tools, None)

    def chat_requiring_a_tool(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        return self._ask(messages, tools, "required")

    def _ask(self, messages: list[dict], tools: list[dict], tool_choice: str | None) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice})
        return self._responses.pop(0)


class SearchArgs(ToolArgs):
    make: OptionalText = None
    price_max: OptionalNumber = None


class LeadArgs(ToolArgs):
    name: Text
    customer_name: Text


class ContactLeadArgs(LeadArgs):
    customer_contact: OptionalText = None


class FakeCarsAdapter(DomainAdapter):
    """Test double for a DomainAdapter: one search tool and a canned execute_tool
    result, recording the call for assertions."""

    domain_type = "cars"
    crm_tag = "Cars"

    def __init__(self, tool_result: dict):
        self._tool_result = tool_result
        self.executed_calls: list[tuple[str, dict]] = []

    def tools(self) -> list[Tool]:
        return [Tool("search_inventory", "Search", SearchArgs)]

    def execute_tool(self, name: str, args: dict) -> dict:
        self.executed_calls.append((name, args))
        return self._tool_result


def test_run_turn_executes_tool_and_grounds_reply():
    tool_result = {"matches": [{"name": "Toyota Corolla", "price": 18000}], "count": 1}
    tool_call = ToolCall(name="search_inventory", arguments={"make": "Toyota", "price_max": 30000})

    first_response = LLMResponse(content=None, tool_calls=[tool_call])
    second_response = LLMResponse(
        content="We have a Toyota Corolla for $18,000 available.", tool_calls=[]
    )

    llm = FakeLLMClient([first_response, second_response])
    adapter = FakeCarsAdapter(tool_result)

    history = [{"role": "user", "content": "do you have any Toyota under 30000"}]

    result = run_turn(history, adapter, llm)

    assert adapter.executed_calls == [("search_inventory", {"make": "Toyota", "price_max": 30000})]

    assert len(llm.calls) == 2

    # The second call must see the tool result, not just the user message; that is what grounds the reply.
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "search_inventory"
    assert json.loads(tool_messages[0]["content"]) == tool_result

    # The assistant message carrying the tool_calls comes before the tool result, and the tool message's
    # tool_call_id matches it.
    assistant_messages = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assistant_tool_calls = assistant_messages[0]["tool_calls"]
    assert len(assistant_tool_calls) == 1
    assert assistant_tool_calls[0]["type"] == "function"
    assert assistant_tool_calls[0]["function"]["name"] == "search_inventory"
    assert json.loads(assistant_tool_calls[0]["function"]["arguments"]) == {
        "make": "Toyota",
        "price_max": 30000,
    }
    assert tool_messages[0]["tool_call_id"] == assistant_tool_calls[0]["id"]

    assistant_index = second_call_messages.index(assistant_messages[0])
    tool_index = second_call_messages.index(tool_messages[0])
    assert assistant_index < tool_index

    # After a search that worked, the model gets one more round that may use tools (a hold or a viewing
    # needs the item the search found); here it just answers.
    assert llm.calls[1]["tools"] == adapter.tool_schemas()

    assert isinstance(result, AgentTurnResult)
    assert result.reply == "We have a Toyota Corolla for $18,000 available."
    assert result.tool_calls_made == [tool_call]
    assert result.tool_results == [tool_result]


def test_run_turn_uses_real_tool_call_id_when_provided():
    tool_result = {"matches": [], "count": 0}
    tool_call = ToolCall(name="search_inventory", arguments={}, id="call_abc123")

    first_response = LLMResponse(content=None, tool_calls=[tool_call])
    second_response = LLMResponse(content="No matches found.", tool_calls=[])

    llm = FakeLLMClient([first_response, second_response])
    adapter = FakeCarsAdapter(tool_result)
    history: list[dict] = [{"role": "user", "content": "anything under 5000?"}]

    run_turn(history, adapter, llm)

    tool_message = next(m for m in history if m.get("role") == "tool")
    assistant_message = next(m for m in history if m.get("role") == "assistant")

    assert tool_message["tool_call_id"] == "call_abc123"
    assert assistant_message["tool_calls"][0]["id"] == "call_abc123"


# Guardrails in the loop


class LeadAdapter(FakeCarsAdapter):
    def tools(self):
        return [*super().tools(), Tool("create_lead", "Lead", LeadArgs)]


def _turn(calls, final="Done."):
    return FakeLLMClient([LLMResponse(content=None, tool_calls=calls), LLMResponse(content=final, tool_calls=[])])


def _tool_messages(history):
    return [json.loads(m["content"]) for m in history if m["role"] == "tool"]


def test_unknown_tool_is_not_executed():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi, I am Sam"}]
    run_turn(history, adapter, _turn([ToolCall(name="delete_everything", arguments={})]))
    assert adapter.executed_calls == []
    assert "error" in _tool_messages(history)[0]


def test_unknown_arguments_are_dropped_before_execution():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi, I am Sam"}]
    run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={"make": "Toyota", "admin": True})]))
    assert adapter.executed_calls == [("search_inventory", {"make": "Toyota"})]


def test_invalid_argument_becomes_error_result_without_executing():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi, I am Sam"}]
    run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={"price_max": -5})]))
    assert adapter.executed_calls == []
    assert "error" in _tool_messages(history)[0]


def test_tool_calls_per_turn_are_capped_but_every_call_gets_a_tool_message():
    adapter = FakeCarsAdapter({"ok": True})
    calls = [ToolCall(name="search_inventory", arguments={"make": f"m{i}"}, id=f"c{i}") for i in range(6)]
    history = [{"role": "user", "content": "hi, I am Sam"}]
    run_turn(history, adapter, _turn(calls))
    assert len(adapter.executed_calls) == guardrails.TOOL_CALL_CAP
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == [f"c{i}" for i in range(6)]


def test_write_tool_is_rate_limited_per_chat():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)

    def lead():
        return ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})

    run_turn([{"role": "user", "content": "a, I am Sam"}], adapter, _turn([lead()]), chat_id=7, write_limiter=limiter)
    history = [{"role": "user", "content": "b, I am Sam"}]
    run_turn(history, adapter, _turn([lead()]), chat_id=7, write_limiter=limiter)
    assert [c[0] for c in adapter.executed_calls] == ["create_lead"]
    assert "limit" in _tool_messages(history)[0]["error"]


def test_write_limit_is_per_chat_id():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)

    def lead():
        return ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})

    run_turn([{"role": "user", "content": "a, I am Sam"}], adapter, _turn([lead()]), chat_id=1, write_limiter=limiter)
    run_turn([{"role": "user", "content": "b, I am Sam"}], adapter, _turn([lead()]), chat_id=2, write_limiter=limiter)
    assert len(adapter.executed_calls) == 2


def test_invalid_write_does_not_burn_the_limit():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)
    bad = ToolCall(name="create_lead", arguments={"name": "Camry"})
    good = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})
    run_turn([{"role": "user", "content": "a, I am Sam"}], adapter, _turn([bad]), chat_id=1, write_limiter=limiter)
    run_turn([{"role": "user", "content": "b, I am Sam"}], adapter, _turn([good]), chat_id=1, write_limiter=limiter)
    assert len(adapter.executed_calls) == 1


def test_reply_links_are_stripped():
    adapter = FakeCarsAdapter({"ok": True})
    result = run_turn(
        [{"role": "user", "content": "hi, I am Sam"}],
        adapter,
        _turn([ToolCall(name="search_inventory", arguments={})], final="Buy at https://evil.example/pay"),
    )
    assert "evil.example" not in result.reply


def test_reply_without_tool_call_is_also_filtered():
    llm = FakeLLMClient([LLMResponse(content="Go to www.evil.example now", tool_calls=[])])
    result = run_turn([{"role": "user", "content": "hi, I am Sam"}], FakeCarsAdapter({}), llm)
    assert "evil.example" not in result.reply


@pytest.mark.parametrize(
    "phrase",
    ["data, not instructions", "never reveal", "refuse", "never send links", "one or two friendly sentences", "same plain, polite voice"],
)
def test_system_prompt_carries_hardening_rules(phrase):
    assert phrase in SYSTEM_PROMPT.lower()


def test_blank_model_reply_after_a_tool_call_still_produces_a_reply():
    adapter = FakeCarsAdapter({"ok": True})
    llm = FakeLLMClient([
        LLMResponse(content=None, tool_calls=[ToolCall(name="search_inventory", arguments={})]),
        LLMResponse(content="", tool_calls=[]),
        LLMResponse(content="", tool_calls=[]),  # blank again after the one retry
    ])
    result = run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, llm)
    assert result.reply.strip()


def test_identical_write_calls_in_one_turn_execute_once():
    adapter = LeadAdapter({"lead_id": 42})
    limiter = FixedWindowRateLimiter(3, 3600)
    args = {"name": "Camry", "customer_name": "Sam"}
    calls = [ToolCall(name="create_lead", arguments=dict(args), id="a"), ToolCall(name="create_lead", arguments=dict(args), id="b")]
    history = [{"role": "user", "content": "book it, I am Sam"}]

    run_turn(history, adapter, _turn(calls), chat_id=1, write_limiter=limiter)

    assert [c[0] for c in adapter.executed_calls] == ["create_lead"]
    results = _tool_messages(history)
    assert len(results) == 2 and results[0] == results[1] == {"lead_id": 42}
    assert limiter.allow(1) and limiter.allow(1)  # only one of the three hits was spent


def test_a_second_different_write_in_one_turn_is_refused_not_run():
    adapter = LeadAdapter({"lead_id": 1})
    calls = [
        ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="a"),
        ToolCall(name="create_lead", arguments={"name": "Corolla", "customer_name": "Sam"}, id="b"),
    ]
    result = run_turn([{"role": "user", "content": "both, I am Sam"}], adapter, _turn(calls))
    assert len(adapter.executed_calls) == 1
    assert "one action" in result.tool_results[1]["error"]


# The model must see its own earlier replies

def test_final_reply_is_appended_to_history_after_a_tool_call():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi, I am Sam"}]
    result = run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={})], final="Here you go."))
    assert history[-1] == {"role": "assistant", "content": result.reply}


def test_final_reply_is_appended_to_history_without_a_tool_call():
    llm = FakeLLMClient([LLMResponse(content="Hello there", tool_calls=[])])
    history = [{"role": "user", "content": "hi, I am Sam"}]
    result = run_turn(history, FakeCarsAdapter({}), llm)
    assert history[-1] == {"role": "assistant", "content": result.reply} == {"role": "assistant", "content": "Hello there"}


# Report tool calls as they happen

class _FailsOnTheReply:
    """First call asks for a lead; the follow-up call that writes the reply blows up."""

    def __init__(self, calls):
        self.calls = calls
        self.n = 0

    def chat(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return LLMResponse(content=None, tool_calls=self.calls)
        raise RuntimeError("provider returned garbage")


def test_executed_tool_calls_are_reported_even_if_the_reply_step_then_fails():
    adapter = LeadAdapter({"lead_id": 42})
    events = []
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="a")

    with pytest.raises(RuntimeError):
        run_turn([{"role": "user", "content": "book it, I am Sam"}], adapter, _FailsOnTheReply([call]), tool_events=events)

    assert [(c.name, r) for c, r in events] == [("create_lead", {"lead_id": 42})]


def test_rejected_calls_are_reported_too_with_their_error():
    adapter = FakeCarsAdapter({"ok": True})
    events = []
    run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, _turn([ToolCall(name="mystery", arguments={})]), tool_events=events)
    assert len(events) == 1 and "error" in events[0][1]


# The customer's own contact details are not lost

class ContactAdapter(FakeCarsAdapter):
    def tools(self):
        return [Tool("create_lead", "Lead", ContactLeadArgs)]


def test_a_phone_the_customer_gave_but_the_model_dropped_is_added_to_the_lead():
    adapter = ContactAdapter({"lead_id": 1})
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Al", "customer_contact": "al@x.com"})
    history = [{"role": "user", "content": "I'll take it. I'm Al, al@x.com, call +971 50 999 8888"}]

    run_turn(history, adapter, _turn([call]), chat_id=1)

    executed = adapter.executed_calls[0][1]
    assert executed["customer_contact"] == "al@x.com +971509998888"


def test_the_reported_tool_event_shows_the_contact_that_was_actually_written():
    """The alert and the dashboard mirror read the event, so it must match what Odoo got."""
    adapter = ContactAdapter({"lead_id": 1})
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Al", "customer_contact": "al@x.com"})
    history = [{"role": "user", "content": "I'm Al, al@x.com, call +971 50 999 8888"}]
    events = []

    run_turn(history, adapter, _turn([call]), chat_id=1, tool_events=events)

    assert events[0][0].arguments["customer_contact"] == "al@x.com +971509998888"


def test_the_turn_result_reports_the_contact_that_was_actually_written():
    """The alert and the dashboard mirror read the returned result on the normal path."""
    adapter = ContactAdapter({"lead_id": 1})
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Al", "customer_contact": "al@x.com"})
    history = [{"role": "user", "content": "I'm Al, al@x.com, call +971 50 999 8888"}]

    result = run_turn(history, adapter, _turn([call]), chat_id=1)

    assert result.tool_calls_made[0].arguments["customer_contact"] == "al@x.com +971509998888"


def test_contact_details_come_only_from_what_the_customer_wrote():
    adapter = ContactAdapter({"lead_id": 1})
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Al"})
    history = [
        {"role": "user", "content": "hello, I am Al"},
        {"role": "assistant", "content": "Reach us on sales@dealer.example or +971 4 123 4567"},
        {"role": "user", "content": "ok"},
    ]

    run_turn(history, adapter, _turn([call]), chat_id=1)

    assert "customer_contact" not in adapter.executed_calls[0][1]


# One more tool round after an argument mistake

class HoldArgs(ToolArgs):
    item_id: WholeNumber
    customer_name: Text


class RetryAdapter(LeadAdapter):
    """Search, lead and hold tools; `fail_on` makes that tool raise when it runs."""

    def __init__(self, tool_result: dict, fail_on: str | None = None):
        super().__init__(tool_result)
        self.fail_on = fail_on

    def tools(self):
        return [*super().tools(), Tool("reserve_item", "Hold", HoldArgs)]

    def execute_tool(self, name, args):
        if name == self.fail_on:
            raise RuntimeError("odoo is down")
        return super().execute_tool(name, args)


def _calls(*calls):
    return LLMResponse(content=None, tool_calls=list(calls))


def _say(text):
    return LLMResponse(content=text, tool_calls=[])


def _bad_search(call_id="bad"):
    return ToolCall(name="search_inventory", arguments={"price_max": "cheap"}, id=call_id)


def _good_search(call_id="good"):
    return ToolCall(name="search_inventory", arguments={"price_max": 20000}, id=call_id)


def test_bad_arguments_get_one_more_round_and_the_corrected_call_runs():
    adapter = RetryAdapter({"matches": [{"id": 4, "name": "Kia Rio", "price": 15000}], "count": 1})
    llm = FakeLLMClient([_calls(_bad_search()), _calls(_good_search()), _say("We have a Kia Rio for $15,000.")])

    result = run_turn([{"role": "user", "content": "cars under 20k"}], adapter, llm)

    assert adapter.executed_calls == [("search_inventory", {"price_max": 20000})]
    assert "Kia Rio" in result.reply
    assert llm.calls[1]["tools"] == adapter.tool_schemas()  # the retry may call tools again
    assert llm.calls[2]["tools"] == adapter.tool_schemas()  # a second extra round may still use tools; here it just answers
    retry_sees = llm.calls[1]["messages"]
    assert "invalid arguments" in json.loads(retry_sees[-1]["content"])["error"]


def test_invalid_json_also_gets_the_retry_round():
    adapter = RetryAdapter({"matches": [], "count": 0})
    broken = ToolCall(name="search_inventory", arguments={INVALID_ARGUMENTS: "{oops"}, id="broken")
    llm = FakeLLMClient([_calls(broken), _calls(_good_search()), _say("Nothing under 20k right now.")])

    run_turn([{"role": "user", "content": "cars under 20k"}], adapter, llm)

    assert adapter.executed_calls == [("search_inventory", {"price_max": 20000})]


def test_a_number_too_big_for_a_float_gets_the_retry_round_instead_of_failing_the_turn():
    adapter = RetryAdapter({"matches": [], "count": 0})
    huge = ToolCall(name="search_inventory", arguments={"price_max": 10**400}, id="huge")
    llm = FakeLLMClient([_calls(huge), _calls(_good_search()), _say("Nothing under 20k right now.")])

    result = run_turn([{"role": "user", "content": "cars under 20k"}], adapter, llm)

    assert "out of range" in result.tool_results[0]["error"]
    assert adapter.executed_calls == [("search_inventory", {"price_max": 20000})]


def test_bad_twice_still_gets_a_last_round_that_may_answer_in_words():
    adapter = RetryAdapter({"ok": True})
    llm = FakeLLMClient([_calls(_bad_search("a")), _calls(_bad_search("b")), _say("What price range did you have in mind?")])

    result = run_turn([{"role": "user", "content": "cheap cars"}], adapter, llm)

    assert adapter.executed_calls == []
    assert len(llm.calls) == 3 and llm.calls[1]["tools"] and llm.calls[2]["tools"]
    assert result.reply == "What price range did you have in mind?"


def test_a_retry_answered_in_words_is_the_reply():
    adapter = RetryAdapter({"ok": True})
    llm = FakeLLMClient([_calls(_bad_search()), _say("What is your budget?")])

    result = run_turn([{"role": "user", "content": "cheap cars"}], adapter, llm)

    assert result.reply == "What is your budget?"
    assert len(llm.calls) == 2


def test_no_retry_once_a_write_has_run():
    adapter = RetryAdapter({"lead_id": 7})
    lead = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="lead")
    llm = FakeLLMClient([_calls(lead, _bad_search()), _say("Done.")])

    run_turn([{"role": "user", "content": "book it, I am Sam"}], adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["create_lead"]
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == []


def test_a_write_that_ran_is_not_repeated_by_the_retry():
    """A bad write and a good write in round one: the good one runs, and the retry never happens,
    so the model cannot use it to place a second action."""
    adapter = RetryAdapter({"lead_id": 7})
    good = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="good")
    bad = ToolCall(name="create_lead", arguments={"name": "Corolla"}, id="bad")
    llm = FakeLLMClient([_calls(good, bad), _say("Done.")])

    run_turn([{"role": "user", "content": "both, I am Sam"}], adapter, llm, chat_id=1)

    assert adapter.executed_calls == [("create_lead", {"name": "Camry", "customer_name": "Sam"})]
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == []


@pytest.mark.parametrize("first_round", [
    [ToolCall(name="search_inventory", arguments={}, id="boom")],  # execution error
    [ToolCall(name="delete_everything", arguments={}, id="unknown")],  # unknown tool
])
def test_errors_other_than_bad_arguments_do_not_trigger_a_retry(first_round):
    adapter = RetryAdapter({"ok": True}, fail_on="search_inventory")
    llm = FakeLLMClient([_calls(*first_round), _say("Sorry, try again later.")])

    run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, llm)

    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == []


def test_a_cap_error_does_not_trigger_a_retry():
    adapter = RetryAdapter({"ok": True})
    calls = [_good_search(f"g{i}") for i in range(guardrails.TOOL_CALL_CAP)] + [_bad_search("over")]
    llm = FakeLLMClient([_calls(*calls), _say("Here you go.")])

    result = run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, llm)

    assert "too many" in result.tool_results[-1]["error"]
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == []


def test_a_limit_error_does_not_trigger_a_retry():
    adapter = RetryAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(0, 3600)
    lead = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="lead")
    llm = FakeLLMClient([_calls(lead), _say("A person will follow up.")])

    result = run_turn([{"role": "user", "content": "book it, I am Sam"}], adapter, llm, chat_id=1, write_limiter=limiter)

    assert "limit" in result.tool_results[0]["error"]
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == []


def test_the_cap_counts_calls_from_both_rounds():
    adapter = RetryAdapter({"ok": True})
    first = [_good_search("g0"), _bad_search("b0")]
    retry = [_good_search(f"r{i}") for i in range(3)]
    llm = FakeLLMClient([_calls(*first), _calls(*retry), _say("Done.")])

    result = run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, llm)

    assert len(adapter.executed_calls) == guardrails.TOOL_CALL_CAP - 1  # g0 and r0; b0 took a slot too
    assert "too many" in result.tool_results[-1]["error"]


def test_after_the_cap_the_next_round_is_still_offered_tools_so_a_write_can_follow():
    adapter = RetryAdapter({"ok": True})
    calls = [_good_search("g0"), _good_search("g1"), _bad_search("b2")]
    llm = FakeLLMClient([_calls(*calls), _say("Done.")])

    run_turn([{"role": "user", "content": "hi, I am Sam"}], adapter, llm)

    # the cap is on searches and one write may follow them; a model that just answers costs no extra call
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] == adapter.tool_schemas()


def test_the_retry_round_is_rate_limited_too():
    adapter = RetryAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(0, 3600)
    bad = ToolCall(name="create_lead", arguments={"name": "Camry"}, id="bad")
    good = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="good")
    llm = FakeLLMClient([_calls(bad), _calls(good), _say("A person will follow up.")])

    result = run_turn([{"role": "user", "content": "book it, I am Sam"}], adapter, llm, chat_id=1, write_limiter=limiter)

    assert adapter.executed_calls == []
    assert "limit" in result.tool_results[1]["error"]


def test_the_retry_round_only_holds_an_item_the_customer_was_shown():
    adapter = RetryAdapter({"lead_id": 1})
    shown = {"role": "tool", "tool_call_id": "old", "name": "search_inventory", "content": json.dumps({"matches": [{"id": 4}]})}
    history = [{"role": "user", "content": "show me"}, shown, {"role": "user", "content": "hold it, I'm Sam"}]
    bad = ToolCall(name="reserve_item", arguments={"item_id": 4.5, "customer_name": "Sam"}, id="bad")
    other = ToolCall(name="reserve_item", arguments={"item_id": 9, "customer_name": "Sam"}, id="other")
    llm = FakeLLMClient([_calls(bad), _calls(other), _say("Which one did you want?")])

    result = run_turn(history, adapter, llm, chat_id=1)

    assert adapter.executed_calls == []
    assert "Nothing was held" in result.tool_results[1]["error"]


def test_the_retry_round_suppresses_a_duplicate_write():
    adapter = RetryAdapter({"lead_id": 3})
    bad = ToolCall(name="create_lead", arguments={"name": "Camry"}, id="bad")
    good = {"name": "Camry", "customer_name": "Sam"}
    retry = [ToolCall(name="create_lead", arguments=dict(good), id="r1"), ToolCall(name="create_lead", arguments=dict(good), id="r2")]
    llm = FakeLLMClient([_calls(bad), _calls(*retry), _say("Done.")])

    result = run_turn([{"role": "user", "content": "book it, I am Sam"}], adapter, llm, chat_id=1)

    assert adapter.executed_calls == [("create_lead", good)]
    assert result.tool_results[1] == result.tool_results[2] == {"lead_id": 3}


def test_calls_and_results_line_up_across_both_rounds():
    adapter = RetryAdapter({"ok": True})
    events = []
    llm = FakeLLMClient([_calls(_bad_search("b")), _calls(_good_search("g")), _say("Done.")])
    history: list[dict] = [{"role": "user", "content": "hi, I am Sam"}]

    result = run_turn(history, adapter, llm, tool_events=events)

    assert [c.id for c in result.tool_calls_made] == ["b", "g"]
    assert "invalid arguments" in result.tool_results[0]["error"] and result.tool_results[1] == {"ok": True}
    assert [(c.id, r) for c, r in events] == list(zip(["b", "g"], result.tool_results, strict=True))
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == ["b", "g"]
    assistant_ids = [tc["id"] for m in history for tc in m.get("tool_calls") or []]
    assert assistant_ids == ["b", "g"]
