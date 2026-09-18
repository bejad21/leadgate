from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import engine.main as main_module
from engine.core.agent_loop import AgentTurnResult
from engine.llm_client import ToolCall
from engine.main import app, config, conversation_history
from engine.rate_limiter import FixedWindowRateLimiter

client = TestClient(app)

VALID_UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "chat": {"id": 12345, "type": "private"},
        "text": "Hello there",
    },
}


@pytest.fixture(autouse=True)
def fake_agent_dependencies(monkeypatch):
    """Prevent the webhook handler's real adapter/LLM construction (which
    would otherwise open a live Odoo XML-RPC connection and call a real LLM
    API) from running during these fast, deterministic unit tests."""
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(
        main_module,
        "run_turn",
        lambda history, adapter, llm: AgentTurnResult(reply="Stubbed grounded reply"),
    )


@pytest.fixture(autouse=True)
def mock_log_turn(monkeypatch):
    """Replace the real MongoDB logging call with a MagicMock everywhere, so
    unit tests never open a real connection to the live MongoDB Atlas
    instance configured in .env. Tests that care about the call shape
    request this fixture explicitly and assert on it."""
    mock = MagicMock()
    monkeypatch.setattr(main_module, "log_turn", mock)
    return mock


@pytest.fixture(autouse=True)
def fresh_rate_limiter(monkeypatch):
    """Each test gets its own rate limiter instance so hits recorded in one
    test (e.g. the rate-limit test itself, which deliberately exhausts the
    limit) can't bleed into unrelated tests via the shared module-level
    singleton."""
    monkeypatch.setattr(
        main_module,
        "_rate_limiter",
        FixedWindowRateLimiter(main_module.RATE_LIMIT_MAX_REQUESTS, main_module.RATE_LIMIT_WINDOW_SECONDS),
    )


def test_webhook_rejects_missing_secret_token():
    response = client.post("/webhook/telegram", json=VALID_UPDATE)

    assert response.status_code == 401


def test_webhook_rejects_wrong_secret_token():
    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )

    assert response.status_code == 401


@respx.mock
def test_webhook_accepts_correct_secret_and_replies():
    conversation_history.clear()
    send_route = respx.post(
        f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))

    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    assert send_route.called
    assert 12345 in conversation_history
    assert conversation_history[12345][0]["content"] == "Hello there"

    # The webhook must send back run_turn's grounded reply, not a placeholder.
    import json as _json

    sent_payload = _json.loads(send_route.calls.last.request.content)
    assert sent_payload["text"] == "Stubbed grounded reply"


@respx.mock
def test_webhook_ignores_non_text_updates_without_crashing():
    conversation_history.clear()
    non_text_update = {"update_id": 2, "edited_message": {"chat": {"id": 999}}}

    response = client.post(
        "/webhook/telegram",
        json=non_text_update,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    assert 999 not in conversation_history


def test_webhook_rejects_bad_secret_even_with_malformed_body():
    """The secret-token check must run before the body is ever parsed, so a
    malformed/malicious body can't be used to skip or crash past auth. This
    covers the non-happy-path: no valid JSON body AND no/wrong secret."""
    response = client.post(
        "/webhook/telegram",
        content=b"not valid json {{{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_webhook_rejects_bad_secret_with_wrong_token_and_malformed_body():
    response = client.post(
        "/webhook/telegram",
        content=b"not valid json {{{",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "wrong-secret",
        },
    )

    assert response.status_code == 401


@respx.mock
def test_webhook_rate_limits_excess_requests_per_chat_id():
    conversation_history.clear()
    respx.post(f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    headers = {"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]}
    limit = main_module.RATE_LIMIT_MAX_REQUESTS

    # Fire more requests than the limit allows from a single chat_id.
    statuses = [
        client.post("/webhook/telegram", json=VALID_UPDATE, headers=headers).status_code
        for _ in range(limit + 5)
    ]

    assert statuses[:limit] == [200] * limit
    assert statuses[limit:] == [429] * 5


@respx.mock
def test_webhook_rate_limit_is_per_chat_id():
    conversation_history.clear()
    respx.post(f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    headers = {"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]}
    limit = main_module.RATE_LIMIT_MAX_REQUESTS

    other_chat_update = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "chat": {"id": 99999, "type": "private"},
            "text": "Hi from a different chat",
        },
    }

    # Exhaust chat 12345's limit.
    for _ in range(limit):
        response = client.post("/webhook/telegram", json=VALID_UPDATE, headers=headers)
        assert response.status_code == 200
    exhausted_response = client.post("/webhook/telegram", json=VALID_UPDATE, headers=headers)
    assert exhausted_response.status_code == 429

    # A different chat_id must be unaffected by chat 12345's exhausted limit.
    other_response = client.post("/webhook/telegram", json=other_chat_update, headers=headers)
    assert other_response.status_code == 200


@respx.mock
def test_webhook_truncates_oversized_message_before_storing_history():
    conversation_history.clear()
    respx.post(f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    oversized_text = "x" * (main_module.MAX_MESSAGE_LENGTH + 500)
    oversized_update = {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "chat": {"id": 55555, "type": "private"},
            "text": oversized_text,
        },
    }

    response = client.post(
        "/webhook/telegram",
        json=oversized_update,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    stored_text = conversation_history[55555][0]["content"]
    assert len(stored_text) == main_module.MAX_MESSAGE_LENGTH
    assert stored_text == "x" * main_module.MAX_MESSAGE_LENGTH


@respx.mock
def test_webhook_leaves_short_message_untouched():
    conversation_history.clear()
    respx.post(f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    assert conversation_history[12345][0]["content"] == "Hello there"


@respx.mock
def test_webhook_logs_turn_to_mongodb_with_correct_shape(monkeypatch, mock_log_turn):
    """Task 3.4: after a real turn completes, log_turn must be called with
    the chat_id, the active domain, the user's message, the agent's reply,
    and the tool calls made -- mocking pymongo indirectly by mocking the
    log_turn function itself, which is the only thing that touches pymongo."""
    conversation_history.clear()
    tool_call = ToolCall(name="search_inventory", arguments={"make": "Toyota"})
    monkeypatch.setattr(
        main_module,
        "run_turn",
        lambda history, adapter, llm: AgentTurnResult(
            reply="We have a Toyota Corolla for $18,000.",
            tool_calls_made=[tool_call],
        ),
    )
    respx.post(f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    assert mock_log_turn.call_count == 1
    call_args = mock_log_turn.call_args.args
    # (mongodb_uri, chat_id, domain_type, message, reply, tool_calls)
    assert call_args[1] == 12345
    assert call_args[2] == config["ACTIVE_DOMAIN"]
    assert call_args[3] == "Hello there"
    assert call_args[4] == "We have a Toyota Corolla for $18,000."
    assert call_args[5] == [tool_call]


@respx.mock
def test_webhook_returns_200_and_does_not_crash_when_run_turn_raises(monkeypatch, mock_log_turn):
    """A fault in run_turn (Odoo down, LLM timeout/429, etc.) must not crash
    the process or return a 500 that would make Telegram retry the same
    update and re-run/re-bill the whole turn."""
    conversation_history.clear()

    def _raise(history, adapter, llm):
        raise RuntimeError("simulated Odoo/LLM failure")

    monkeypatch.setattr(main_module, "run_turn", _raise)

    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    mock_log_turn.assert_not_called()


@respx.mock
def test_webhook_returns_200_and_does_not_crash_when_send_message_raises(monkeypatch, mock_log_turn):
    """A fault in the Telegram API call itself must not crash the process
    or return a 500 either."""
    conversation_history.clear()

    def _raise(chat_id, text):
        raise RuntimeError("simulated Telegram API failure")

    monkeypatch.setattr(main_module, "send_message", _raise)

    response = client.post(
        "/webhook/telegram",
        json=VALID_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]},
    )

    assert response.status_code == 200
    mock_log_turn.assert_not_called()


def test_trim_conversation_history_keeps_only_last_n_user_turns():
    """Task 6.x fix: conversation_history must not grow forever. Confirms
    old turns actually get dropped once the cap is exceeded, while the
    system prompt (if present) is always preserved."""
    conversation_history.clear()
    chat_id = 424242
    max_turns = main_module.CONVERSATION_HISTORY_MAX_TURNS

    history = [{"role": "system", "content": "system prompt"}]
    for i in range(max_turns + 10):
        history.append({"role": "user", "content": f"message {i}"})
        history.append({"role": "assistant", "content": f"reply {i}"})
    conversation_history[chat_id] = history

    main_module._trim_conversation_history(chat_id)

    trimmed = conversation_history[chat_id]
    user_messages = [m for m in trimmed if m["role"] == "user"]
    assert len(user_messages) == max_turns
    # The oldest turns were dropped, the most recent ones were kept.
    assert user_messages[0]["content"] == f"message {10}"
    assert user_messages[-1]["content"] == f"message {max_turns + 9}"
    # The system prompt survives trimming and stays first.
    assert trimmed[0] == {"role": "system", "content": "system prompt"}
