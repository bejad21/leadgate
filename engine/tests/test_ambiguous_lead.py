"""A lead is not created for an item description that fits several catalog items, unless the customer
said which (a price or a place). The model is told to ask instead of choosing for them."""

import json

import pytest

from engine.core.agent_loop import run_turn
from engine.llm_client import ToolCall
from engine.tests.test_agent_loop import FakeLLMClient, RetryAdapter, _calls, _say


def _row(id_, price, city):
    return {
        "id": id_,
        "name": "2024 Volkswagen Atlas",
        "price": float(price),
        "attributes": json.dumps({"make": "Volkswagen", "model": "Atlas", "year": "2024", "location": f"{city}, OH"}),
    }


TWO_ATLASES = {"matches": [_row(1, 32550, "Dayton"), _row(2, 41200, "Toledo")], "count": 2}
ONE_ATLAS = {"matches": [_row(1, 32550, "Dayton")], "count": 1}


def _search():
    return ToolCall(name="search_inventory", arguments={"make": "Volkswagen", "model": "Atlas"}, id="s1")


def _lead(name="2024 Volkswagen Atlas"):
    return ToolCall(name="create_lead", arguments={"name": name, "customer_name": "Ana"}, id="l1")


def _run(result, text, lead=None):
    adapter = RetryAdapter(result)
    llm = FakeLLMClient([_calls(_search()), _calls(lead or _lead()), _say("done")])
    outcome = run_turn([{"role": "user", "content": text}], adapter, llm, chat_id=1)
    return adapter, outcome


def test_two_matching_items_and_no_choice_means_no_lead():
    adapter, outcome = _run(TWO_ATLASES, "Put me down for the 2024 Volkswagen Atlas. Ana, ana@example.com")

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory"]
    assert "Several items match" in outcome.tool_results[1]["error"]
    assert "which one" in outcome.tool_results[1]["error"]


@pytest.mark.parametrize(
    "text",
    [
        "Put me down for the 2024 Volkswagen Atlas at 41200. Ana, ana@example.com",
        "I'll take the 2024 Volkswagen Atlas for $41,200. Ana, ana@example.com",
        "the 2024 Volkswagen Atlas in Toledo please. Ana, ana@example.com",
    ],
)
def test_a_price_or_place_that_picks_one_item_lets_the_lead_through(text):
    adapter, outcome = _run(TWO_ATLASES, text)

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "create_lead"]
    assert "error" not in outcome.tool_results[1]


def test_one_matching_item_is_not_ambiguous():
    adapter, _ = _run(ONE_ATLAS, "Put me down for the 2024 Volkswagen Atlas. Ana, ana@example.com")

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "create_lead"]


def test_matches_with_other_names_do_not_block_a_lead_for_a_different_item():
    result = {"matches": [_row(1, 32550, "Dayton"), _row(2, 41200, "Toledo")], "count": 2}
    adapter, _ = _run(result, "I want the Mazda. Ana, ana@example.com", lead=_lead(name="2021 Mazda CX-5"))

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "create_lead"]


def test_a_lead_with_no_search_this_turn_is_not_blocked():
    adapter = RetryAdapter(TWO_ATLASES)
    llm = FakeLLMClient([_calls(_lead()), _say("done")])

    run_turn([{"role": "user", "content": "I'll take the cheaper one. Ana, ana@example.com"}], adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["create_lead"]
