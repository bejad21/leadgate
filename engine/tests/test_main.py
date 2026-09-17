import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import engine.main as main_module
from engine.core.agent_loop import AgentTurnResult
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
