"""A customer who names an item and asks to hold it in one message needs two steps: find the item,
then hold it. The model gets one more round with tools after a search that worked."""

import json

from engine.core.agent_loop import run_turn
from engine.llm_client import ToolCall
from engine.tests.test_agent_loop import FakeLLMClient, RetryAdapter, _calls, _say


def _search(call_id="s1"):
    return ToolCall(name="search_inventory", arguments={"make": "Toyota"}, id=call_id)


def _hold(item_id=4, call_id="h1"):
    return ToolCall(name="reserve_item", arguments={"item_id": item_id, "customer_name": "Sam"}, id=call_id)


FOUND = {"matches": [{"id": 4, "name": "Toyota Camry", "price": 32550}], "count": 1}


def test_a_search_that_worked_is_followed_by_a_round_that_can_use_tools():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _say("We have a Camry for $32,550.")])

    result = run_turn([{"role": "user", "content": "any Toyota?"}], adapter, llm)

    assert result.reply == "We have a Camry for $32,550."
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] == adapter.tool_schemas()


def test_find_then_hold_in_one_message_holds_the_item_found_this_turn():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _calls(_hold()), _say("Requested. A person will confirm.")])

    result = run_turn([{"role": "user", "content": "hold the Camry for me, I'm Sam 555-0100"}], adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "reserve_item"]
    assert "error" not in result.tool_results[1]
    assert llm.calls[2]["tools"] == []  # the reply is written without tools


def test_an_item_that_no_search_showed_is_still_refused():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _calls(_hold(item_id=99)), _say("Which one did you want?")])

    result = run_turn([{"role": "user", "content": "hold item 99, I'm Sam"}], adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory"]
    assert "Nothing was held" in result.tool_results[1]["error"]


def test_at_most_two_extra_rounds_after_a_search():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search("a")), _calls(_search("b")), _calls(_search("c")), _say("Here they are.")])

    run_turn([{"role": "user", "content": "any Toyota?"}], adapter, llm)

    assert len(llm.calls) == 4
    assert llm.calls[2]["tools"]  # the second extra round may call tools
    assert llm.calls[3]["tools"] == []  # after that the reply is written without tools


def test_no_extra_round_after_a_failed_search():
    adapter = RetryAdapter(FOUND, fail_on="search_inventory")
    llm = FakeLLMClient([_calls(_search()), _say("Sorry, search is down.")])

    run_turn([{"role": "user", "content": "any Toyota?"}], adapter, llm)

    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] == []


def test_ids_from_this_turns_search_are_in_the_history_the_next_call_sees():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _say("Done.")])

    run_turn([{"role": "user", "content": "any Toyota?"}], adapter, llm)

    tool_message = next(m for m in llm.calls[1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["matches"][0]["id"] == 4


# A model that returns nothing, or keeps asking for tools, after the search

from engine.core.guardrails import EMPTY_REPLY, NO_MATCH_REPLY  # noqa: E402
from engine.llm_client import LLMResponse  # noqa: E402

NOTHING = {"matches": [], "count": 0}


def _blank():
    return LLMResponse(content=None, tool_calls=[])


def test_a_search_with_no_matches_and_a_blank_model_gets_an_honest_reply_not_didnt_catch_that():
    adapter = RetryAdapter(NOTHING)
    llm = FakeLLMClient([_calls(_search()), _blank(), _blank(), _blank()])

    result = run_turn([{"role": "user", "content": "do you have a Cybertruck"}], adapter, llm)

    assert result.reply == NO_MATCH_REPLY
    assert "catalog" in result.reply
    assert len(llm.calls) <= 4


def test_blank_model_after_matches_still_falls_back_to_the_polite_reply():
    adapter = RetryAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _blank(), _blank(), _blank()])

    assert run_turn([{"role": "user", "content": "any Toyota?"}], adapter, llm).reply == EMPTY_REPLY


def test_the_last_attempt_asks_the_model_to_answer_in_words():
    adapter = RetryAdapter(NOTHING)
    llm = FakeLLMClient([_calls(_search()), _calls(_search("b")), _blank(), _say("Sorry, we have no Cybertruck.")])

    result = run_turn([{"role": "user", "content": "do you have a Cybertruck"}], adapter, llm)

    assert result.reply == "Sorry, we have no Cybertruck."
    last = llm.calls[3]
    assert last["tools"] == []
    # after tool results the note is a user message: NVIDIA's hosted Mistral answers a system message there with a 400
    assert last["messages"][-1]["role"] == "user" and "plain words" in last["messages"][-1]["content"]
    assert all("plain words" not in str(m.get("content")) for m in llm.calls[2]["messages"])
