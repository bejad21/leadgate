import httpx
import respx
from fastapi.testclient import TestClient

from engine.main import app, config, conversation_history

client = TestClient(app)

VALID_UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "chat": {"id": 12345, "type": "private"},
        "text": "Hello there",
    },
}


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
