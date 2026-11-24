"""Working out which item "the third one" means, in code, from what the customer was shown.

The case that started it: four Cadillacs were listed in one sentence, the customer said "reserve the third one", and the
model held the fourth."""
import json

from engine.core.adapter_base import DomainAdapter
from engine.core.agent_loop import _reference_note, _shown_for_buttons, run_turn
from engine.core.references import (
    last_listed,
    order_by_appearance,
    position_in,
    refers_to_the_one_in_view,
    resolve_reference,
)
from engine.core.tools import Tool
from engine.llm_client import ToolCall
from engine.tests.test_agent_habits import ReserveArgs, SearchArgs, calls, says
from engine.tests.test_agent_loop import FakeLLMClient

XT4 = {"id": 310, "name": "2019 Cadillac XT4", "price": 21237.0, "attributes": "{}"}
XT5_OLD = {"id": 311, "name": "2022 Cadillac XT5", "price": 31820.0, "attributes": "{}"}
XT5_NEW = {"id": 315, "name": "2024 Cadillac XT5", "price": 40850.0, "attributes": "{}"}
LYRIQ = {"id": 140, "name": "2024 Cadillac Lyriq", "price": 39780.0, "attributes": "{}"}
CADILLACS = [XT4, XT5_OLD, XT5_NEW, LYRIQ]  # the order a search returns them

PROSE_LIST = (
    "Four Cadillacs: 2019 Cadillac XT4, $21,237, 56,574 miles, Doral, FL; 2022 Cadillac XT5, $31,820, 32,544 miles, "
    "Cambridge, OH; 2024 Cadillac XT5, $40,850, 16,797 miles, Cincinnati, OH; 2024 Cadillac Lyriq, $39,780, 23,305 miles, "
    "Cincinnati, OH."
)
# What the model wrote when it ordered the same cars by price: the screen order differs from the search order.
PRICE_ORDER_LIST = "1. 2019 Cadillac XT4 - $21,237\n2. 2022 Cadillac XT5 - $31,820\n3. 2024 Cadillac Lyriq - $39,780\n4. 2024 Cadillac XT5 - $40,850"


def resolved(text, history, extra=()):
    row = resolve_reference(text, history, extra)
    assert row is not None, text
    return row


def text_history(listing=PROSE_LIST, ask="Please reserve the third one for me. Hugo Marsh, 513 555 0188"):
    return [
        {"role": "user", "content": "what Cadillacs do you have?"},
        {"role": "assistant", "content": listing},
        {"role": "user", "content": ask},
    ]


def tool_message(rows):
    return {"role": "tool", "tool_call_id": "t", "name": "search_inventory", "content": json.dumps({"matches": rows, "count": len(rows)})}


# Reading a position out of the customer's words


def test_positions_are_read_from_words_and_numbers():
    assert position_in("Please reserve the third one for me") == 3
    assert position_in("the 2nd car") == 2
    assert position_in("number 3 please") == 3
    assert position_in("#4") == 4
    assert position_in("show me the last one") == -1
    assert position_in("Option 1 looks good") == 1


def test_a_count_or_a_figure_is_not_a_position():
    assert position_in("I want 3 bedrooms") is None
    assert position_in("3 beds under 400k") is None
    assert position_in("Hold it, my number is 5551234") is None
    assert position_in("what is the price?") is None


def test_that_one_is_recognised_as_a_pointer_to_the_item_in_view():
    assert refers_to_the_one_in_view("I'll take that one")
    assert not refers_to_the_one_in_view("anything under 20k?")


# The order the customer saw


def test_items_are_ordered_by_where_their_price_is_written():
    assert [r["id"] for r in order_by_appearance(PRICE_ORDER_LIST, CADILLACS)] == [310, 311, 140, 315]
    assert [r["id"] for r in order_by_appearance(PROSE_LIST, CADILLACS)] == [310, 311, 315, 140]


def test_a_price_inside_a_longer_number_is_not_a_match():
    rows = [{"id": 1, "name": "A", "price": 2000.0}, {"id": 2, "name": "B", "price": 20000.0}]
    assert [r["id"] for r in order_by_appearance("Only B at $20,000", rows)] == [2]


def test_two_items_with_the_same_price_keep_their_order():
    rows = [{"id": 1, "name": "A", "price": 5000.0}, {"id": 2, "name": "B", "price": 5000.0}]
    assert [r["id"] for r in order_by_appearance("1. A $5,000\n2. B $5,000", rows)] == [1, 2]


def test_the_last_listed_items_come_from_the_most_recent_message_that_names_any():
    history = [
        tool_message(CADILLACS),
        {"role": "assistant", "content": PROSE_LIST},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "You're welcome!"},
        {"role": "user", "content": "the third one"},
    ]
    assert [r["id"] for r in last_listed(history)] == [310, 311, 315, 140]


# Turning the words into an item


def test_the_third_one_is_the_third_on_the_screen_even_when_the_search_order_differs():
    history = [tool_message(CADILLACS), {"role": "assistant", "content": PRICE_ORDER_LIST}]
    assert resolved("the third one", history)["id"] == 140  # the Lyriq was third on screen
    assert resolved("the last one", history)["id"] == 315


def test_it_resolves_from_text_only_history_once_this_turns_search_supplied_the_rows():
    history = text_history()
    assert resolve_reference("the third one", history) is None  # no rows yet: the history is words only
    assert resolved("the third one", history, CADILLACS)["id"] == 315


def test_a_position_beyond_the_list_or_a_list_of_one_resolves_to_nothing():
    history = [tool_message(CADILLACS), {"role": "assistant", "content": PROSE_LIST}]
    assert resolve_reference("the fifth one", history) is None
    one = [tool_message([XT4]), {"role": "assistant", "content": "Only this: 2019 Cadillac XT4, $21,237."}]
    assert resolve_reference("the first one", one) is None  # one item has no positions
    assert resolved("I'll take that one", one)["id"] == 310


def test_that_one_is_not_resolved_when_several_items_were_in_view():
    history = [tool_message(CADILLACS), {"role": "assistant", "content": PROSE_LIST}]
    assert resolve_reference("I'll take that one", history) is None


def test_the_note_names_the_item_and_its_id():
    note = _reference_note(XT5_NEW)
    assert "315" in note and "2024 Cadillac XT5" in note and "$40,850" in note


# In the agent loop: the hold goes to the item that was meant


class CadillacAdapter(DomainAdapter):
    domain_type = "cars"
    crm_tag = "Cars"

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def tools(self):
        return [Tool("search_inventory", "Search", SearchArgs), Tool("reserve_item", "Hold an item", ReserveArgs)]

    def execute_tool(self, name, args):
        self.executed.append((name, args))
        if name == "reserve_item":
            item = next(r for r in CADILLACS if r["id"] == args["item_id"])
            return {"lead_id": 9, "kind": "reservation", "item_name": item["name"], "detail": "Held for 24 hours"}
        return {"matches": CADILLACS, "count": 4}


def hold(item_id):
    return ("reserve_item", {"item_id": item_id, "customer_name": "Hugo Marsh", "customer_contact": "513 555 0188"})


def test_the_fourth_car_is_not_held_when_the_customer_said_the_third_one():
    """The model searched, then held the Lyriq (the fourth) for "the third one"."""
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), calls(hold(LYRIQ["id"])), says("I have passed on your request to hold the 2024 Cadillac Lyriq.")])
    adapter = CadillacAdapter()
    result = run_turn(text_history(), adapter, llm)
    held = [args for name, args in adapter.executed if name == "reserve_item"]
    assert held and held[0]["item_id"] == XT5_NEW["id"]  # the third on the screen, not the model's pick
    assert "2024 Cadillac XT5" in result.reply and "Lyriq" not in result.reply  # the reply names what was really held


def test_a_right_pick_goes_through_untouched():
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), calls(hold(XT5_NEW["id"])), says("Done.")])
    adapter = CadillacAdapter()
    result = run_turn(text_history(), adapter, llm)
    assert [args["item_id"] for name, args in adapter.executed if name == "reserve_item"] == [315]
    assert result.reply == "Done."  # nothing was corrected, so the model's words stand


def test_an_item_outside_the_list_the_customer_saw_is_left_alone():
    other = {"id": 999, "name": "2021 Cadillac CT5", "price": 28000.0, "attributes": "{}"}

    class WithOther(CadillacAdapter):
        def execute_tool(self, name, args):
            if name == "reserve_item":
                self.executed.append((name, args))
                return {"lead_id": 9, "kind": "reservation", "item_name": other["name"], "detail": "Held for 24 hours"}
            self.executed.append((name, args))
            return {"matches": [*CADILLACS, other], "count": 5}

    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), calls(hold(999)), says("Done.")])
    adapter = WithOther()
    run_turn(text_history(ask="Hold the CT5 instead of the third one. Hugo Marsh, 513 555 0188"), adapter, llm)
    assert [args["item_id"] for name, args in adapter.executed if name == "reserve_item"] == [999]


def test_with_live_history_the_model_is_told_which_item_the_words_point_at_before_it_answers():
    history = [
        {"role": "user", "content": "what Cadillacs do you have?"},
        tool_message(CADILLACS),
        {"role": "assistant", "content": PROSE_LIST},
        {"role": "user", "content": "tell me about the third one"},
    ]
    llm = FakeLLMClient([says("It is a 2024 XT5.")])
    run_turn(history, CadillacAdapter(), llm)
    first = llm.calls[0]["messages"]
    assert first[-1]["role"] == "system" and "315" in first[-1]["content"]  # the note, which is not kept in the chat
    assert all("315" not in str(m.get("content")) for m in history if m.get("role") == "system")


def test_no_note_is_added_when_the_words_point_at_no_position():
    history = [tool_message(CADILLACS), {"role": "assistant", "content": PROSE_LIST}, {"role": "user", "content": "do you have any Fords?"}]
    llm = FakeLLMClient([says("No Fords.")])
    run_turn(history, CadillacAdapter(), llm)
    assert llm.calls[0]["messages"][-1]["role"] == "user"


# What a front end may put buttons under


def test_the_items_a_reply_writes_out_are_offered_in_the_order_it_writes_them():
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), says(PRICE_ORDER_LIST)])
    result = run_turn([{"role": "user", "content": "what Cadillacs do you have?"}], CadillacAdapter(), llm)
    assert [r["id"] for r in result.shown] == [310, 311, 140, 315]


def test_a_reply_that_names_no_item_offers_nothing():
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), says("I found some, which budget do you have?")])
    assert run_turn([{"role": "user", "content": "Cadillacs?"}], CadillacAdapter(), llm).shown == []


def test_a_turn_that_acted_offers_nothing():
    llm = FakeLLMClient([calls(("search_inventory", {"make": "Cadillac"})), calls(hold(XT5_NEW["id"])), says("Passed on. The 2024 XT5 is $40,850.")])
    assert run_turn(text_history(), CadillacAdapter(), llm).shown == []


def test_shown_for_buttons_reads_only_the_turns_last_search():
    older = {"matches": [XT4], "count": 1}
    newer = {"matches": CADILLACS, "count": 4}

    class Adapter:
        write_tools = frozenset({"reserve_item"})

    got = _shown_for_buttons(PROSE_LIST, [older, newer], [ToolCall(name="search_inventory", arguments={}, id="a")], Adapter())
    assert [r["id"] for r in got] == [310, 311, 315, 140]
