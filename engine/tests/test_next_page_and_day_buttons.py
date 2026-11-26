"""The "Next 5" button under a long list, and the day buttons after a viewing is asked for in words without a day."""
import datetime as dt

import pytest

from engine import buttons
from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.core.agent_loop import AgentTurnResult, run_turn
from engine.core.paging import latest_search, next_page
from engine.core.references import resolve_reference
from engine.llm_client import LLMResponse, ToolCall
from engine.main import conversation_history
from engine.tests.fakes import FakeOdoo, item
from engine.tests.test_agent_habits import CAMRY, ShopAdapter, earlier_search
from engine.tests.test_agent_loop import FakeLLMClient
from engine.tests.test_main_buttons import CHAT, XT5, keyboard_data, sent, setup, tap, telegram, typed  # noqa: F401


def cars(count):
    return {n: item(n, name=f"2020 Toyota Camry {n}", price=20000.0 + n) for n in range(1, count + 1)}


def says(text):
    return LLMResponse(content=text, tool_calls=[])


def searches(name="search_inventory", **arguments):
    return LLMResponse(content=None, tool_calls=[ToolCall(name=name, arguments=arguments, id="s1")])


# Adapters give the next page


@pytest.mark.parametrize("adapter_class, tool", [(CarsAdapter, "search_inventory"), (RealEstateAdapter, "search_listings")])
def test_an_offset_returns_the_following_matches_and_the_full_count(adapter_class, tool):
    rows = cars(12)
    for row in rows.values():
        row["domain_type"] = adapter_class.domain_type
    adapter = adapter_class(FakeOdoo(items=rows))
    page = adapter.execute_tool(tool, {"_offset": 5})
    assert [r["id"] for r in page["matches"]] == [6, 7, 8, 9, 10] and page["count"] == 12
    assert [r["id"] for r in adapter.execute_tool(tool, {})["matches"]] == [1, 2, 3, 4, 5]


def test_the_model_cannot_ask_for_a_page_itself():
    models = CarsAdapter(FakeOdoo()).tool_models()
    assert "_offset" not in models["search_inventory"].model_validate({"_offset": 500}).model_dump(exclude_none=True)


# The turn says there is a next page


def long_search_turn(count=12):
    adapter = CarsAdapter(FakeOdoo(items=cars(count)))
    history = [{"role": "user", "content": "what Toyotas do you have?"}]
    reply = "\n".join(f"{n}. 2020 Toyota Camry {n}, ${20000 + n:,}" for n in range(1, min(count, 5) + 1))
    return history, adapter, run_turn(history, adapter, FakeLLMClient([searches(make="Toyota"), says(reply)]))


def test_a_list_with_more_matches_offers_the_next_page():
    history, adapter, result = long_search_turn()
    assert result.more is not None and result.more == next_page(history, adapter.write_tools, 5) and result.more["offset"] == 5
    found = latest_search(history, adapter.write_tools)
    assert found is not None and found[:2] == ("search_inventory", {"make": "Toyota"})


def next_key(result) -> str:
    assert result.more is not None
    return result.more["key"]


def viewed_item(result) -> int:
    assert result.viewing_item is not None
    return result.viewing_item["id"]


def test_a_list_that_is_complete_offers_nothing():
    assert long_search_turn(count=4)[2].more is None


def test_the_next_button_is_the_last_row_and_fits_telegrams_limit():
    keyboard = buttons.results_keyboard([XT5, {**XT5, "id": 316}], {"offset": 5, "key": "0a1b2c3d"})
    assert keyboard is not None and keyboard["inline_keyboard"][-1] == [{"text": "Next 5", "callback_data": "more:5:0a1b2c3d"}]
    assert buttons.parse_callback("more:5:0a1b2c3d") == ("more", 5, ["0a1b2c3d"])


@pytest.mark.parametrize("data", ["more:5", "more:-5:0a1b2c3d", "more:5:xyz", "more:99999:0a1b2c3d", "more:٥:0a1b2c3d"])
def test_a_forged_next_button_is_not_ours(data):
    assert buttons.parse_callback(data) is None


# The tap sends the next page


@pytest.fixture
def paged(monkeypatch, setup):  # noqa: F811
    import engine.main as main_module

    history, adapter, result = long_search_turn()
    adapter.get_item = lambda item_id: adapter.odoo.items.get(item_id)
    monkeypatch.setattr(main_module, "_get_adapter", lambda: adapter)
    conversation_history[CHAT] = history
    return history, adapter, result


def test_the_tap_sends_the_following_five_from_the_catalog_without_the_model(telegram, paged, setup):  # noqa: F811
    history, _, result = paged
    assert tap(f"more:5:{next_key(result)}").status_code == 200
    payload = sent(telegram)[0]
    assert payload["text"].startswith("Matches 6 to 10 of 12:")
    assert "2020 Toyota Camry 6" in payload["text"] and "$20,010" in payload["text"]
    assert keyboard_data(payload) == [f"pick:{n}" for n in range(6, 11)] + [f"more:10:{next_key(result)}"]
    assert setup[1].calls == []


def test_the_last_page_has_no_next_button_and_a_position_means_that_page(telegram, paged):  # noqa: F811
    history, _, result = paged
    tap(f"more:10:{next_key(result)}", update_id=2)
    assert keyboard_data(sent(telegram)[0]) == ["pick:11", "pick:12"]
    pointed = resolve_reference("hold the second one", conversation_history[CHAT])
    assert pointed is not None and pointed["id"] == 12


def test_a_next_button_from_an_older_search_is_out_of_date(telegram, paged):  # noqa: F811
    import engine.main as main_module

    tap("more:5:ffffffff")
    assert sent(telegram)[0]["text"] == main_module.TAP_OUT_OF_DATE


def test_after_a_chat_with_no_search_the_next_button_is_out_of_date(telegram):  # noqa: F811
    import engine.main as main_module

    tap("more:5:0a1b2c3d")
    assert sent(telegram)[0]["text"] == main_module.TAP_OUT_OF_DATE


# Day buttons after a typed viewing request


def viewing_turn(text, history=None, model_says="Sure. Which day suits you?"):
    chat = [*(history or earlier_search()), {"role": "user", "content": text}]
    return run_turn(chat, ShopAdapter(), FakeLLMClient([says(model_says)]))


@pytest.mark.parametrize("text", [
    "I'd like to view the first one",
    "can I come and see the first one?",
    "book a viewing for the first one",
    "can I test drive number 1?",
])
def test_a_viewing_asked_for_without_a_day_names_the_item_for_the_day_buttons(text):
    assert viewed_item(viewing_turn(text)) == CAMRY["id"], text


def test_the_only_item_in_view_is_the_one_meant():
    one = earlier_search()
    one[-1] = {"role": "assistant", "content": "Only the 2020 Toyota Camry at $21,834."}
    assert viewed_item(viewing_turn("I'd like to book a viewing", history=one)) == CAMRY["id"]


@pytest.mark.parametrize("text", [
    "I'd like to view the first one on Saturday",
    "can I see more options?",
    "let me see the cheaper ones",
    "I'd like to book a viewing",  # two items were shown and none is named
])
def test_no_day_buttons_when_a_day_was_given_or_the_item_is_not_clear(text):
    assert viewing_turn(text).viewing_item is None, text


def test_the_webhook_puts_the_day_buttons_under_the_reply(telegram, setup):  # noqa: F811
    catalog, turns = setup
    turns.result = AgentTurnResult(reply="Which day suits you?", viewing_item={"id": 315, "name": "2024 Cadillac XT5"})
    typed("I'd like to view the XT5")
    today = dt.date.today()
    assert keyboard_data(sent(telegram)[0])[:2] == [f"day:315:{today.isoformat()}", f"day:315:{(today + dt.timedelta(days=1)).isoformat()}"]


def test_no_day_buttons_for_an_item_that_is_no_longer_available(telegram, setup):  # noqa: F811
    setup[1].result = AgentTurnResult(reply="Which day suits you?", viewing_item={"id": 77, "name": "2020 Toyota Camry"})
    typed("I'd like to view the Camry")
    assert "reply_markup" not in sent(telegram)[0]
