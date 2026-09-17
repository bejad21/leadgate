import json

import httpx
import respx

from engine.telegram_client import extract_message, send_message


@respx.mock
def test_send_message_posts_to_telegram_api(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    route = respx.post("https://api.telegram.org/bottest-bot-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    response = send_message(chat_id=42, text="hello")

    assert route.called
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"chat_id": 42, "text": "hello"}
    assert response.status_code == 200


def test_extract_message_returns_chat_id_and_text():
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 555, "type": "private"},
            "text": "hi there",
        },
    }

    result = extract_message(update)

    assert result == (555, "hi there")


def test_extract_message_returns_none_for_non_message_update():
    update = {"update_id": 2, "callback_query": {"id": "abc"}}

    assert extract_message(update) is None


def test_extract_message_returns_none_for_message_without_text():
    update = {
        "update_id": 3,
        "message": {
            "message_id": 2,
            "chat": {"id": 555, "type": "private"},
            "photo": [{"file_id": "abc"}],
        },
    }

    assert extract_message(update) is None


def test_extract_message_returns_none_for_malformed_update():
    assert extract_message({}) is None
    assert extract_message({"message": "not-a-dict"}) is None
    assert extract_message({"message": {"chat": "not-a-dict", "text": "hi"}}) is None
