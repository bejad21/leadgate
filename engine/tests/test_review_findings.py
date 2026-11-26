"""Edge cases found while reviewing the changes for the open issues."""
import json

import pytest

from engine import store as store_module
from engine.core.agent_loop import ASK_NAME_TOO, AgentTurnResult, run_turn
from engine.core.paging import latest_search
from engine.llm_client import LLMResponse, ToolCall
from engine.store import InMemoryStore, SupabaseStore
from engine.tests.fake_postgrest import FakePostgrest
from engine.tests.test_agent_habits import ShopAdapter, says
from engine.tests.test_agent_loop import FakeLLMClient
from engine.tests.test_main_buttons import CHAT, sent, setup, telegram, typed  # noqa: F401
from engine.tests.test_search_completion import adapter as homes


def test_the_next_page_repeats_the_search_with_the_place_that_was_added():
    chat_adapter = homes()
    history = [{"role": "user", "content": "is the Eliot house still available?"}]
    llm = FakeLLMClient([
        LLMResponse(content=None, tool_calls=[ToolCall(name="search_listings", arguments={"price_max": 900000}, id="s1")]),
        says("Checked."),
    ])
    run_turn(history, chat_adapter, llm)
    found = latest_search(history, chat_adapter.write_tools)
    assert found is not None and found[1] == {"price_max": 900000, "location": "Eliot"}


@pytest.mark.parametrize("text", [
    "Please don't call me, just show me cars",
    "do not phone me",
    "No need to call me back",
])
def test_a_customer_who_says_not_to_call_is_not_asked_for_a_name(text):
    reply = run_turn([{"role": "user", "content": text}], ShopAdapter(), FakeLLMClient([says("Understood.")])).reply
    assert ASK_NAME_TOO not in reply, text


def test_what_happened_in_an_undelivered_turn_is_still_known_to_the_next(telegram, setup, monkeypatch):  # noqa: F811
    import httpx

    memory = InMemoryStore()
    store_module.set_store(memory)
    try:
        typed("first", update_id=1)
        telegram["send"].mock(return_value=httpx.Response(500))
        setup[1].result = AgentTurnResult(reply="held it")
        typed("hold it", update_id=2)
        telegram["send"].mock(return_value=httpx.Response(200, json={"ok": True}))
        typed("did that work?", update_id=3)
        said = [m["content"] for m in setup[1].calls[-1]["history"] if m["role"] == "user"]
        assert said == ["first", "hold it", "did that work?"]
        assert "did that work?" in (memory.load_state(CHAT) or "")
    finally:
        store_module.set_store(None)


def test_the_readiness_ping_notices_a_missing_migration():
    fake = FakePostgrest()
    SupabaseStore("https://proj.supabase.co", "k", client=fake.client()).ping()
    assert fake.requests[-1].url.path.endswith("/chat_state")


def test_the_saved_state_is_json_the_engine_can_read_back(telegram, setup):  # noqa: F811
    memory = InMemoryStore()
    store_module.set_store(memory)
    try:
        typed("hello")
        assert json.loads(memory.load_state(CHAT) or "null")[0]["content"] == "hello"
        assert sent(telegram)
    finally:
        store_module.set_store(None)
