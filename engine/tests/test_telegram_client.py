import json

import httpx
import respx

import pytest

from engine.telegram_client import extract_message, format_for_telegram, send_message


@respx.mock
def test_send_message_posts_to_telegram_api(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    route = respx.post("https://api.telegram.org/bottest-bot-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    response = send_message(chat_id=42, text="hello")

    assert route.called
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"chat_id": 42, "text": "hello", "parse_mode": "HTML"}
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


# ---- formatting: the model writes Markdown, Telegram wants its own HTML -------

@pytest.mark.parametrize(
    "markdown, html",
    [
        ("**2020 Toyota Camry** - $21,834", "<b>2020 Toyota Camry</b> - $21,834"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("- first\n- second", "• first\n• second"),
        ("* first\n* second", "• first\n• second"),
        ("### Your options", "<b>Your options</b>"),
        ("1. **Camry** - $21,834", "1. <b>Camry</b> - $21,834"),
        ("half **open bold", "half **open bold"),
        ("**bold** and **more**", "<b>bold</b> and <b>more</b>"),
        ("plain text stays", "plain text stays"),
    ],
)
def test_markdown_becomes_telegram_html(markdown, html):
    assert format_for_telegram(markdown) == html


@respx.mock
def test_send_message_falls_back_to_plain_text_when_telegram_rejects_the_markup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    route = respx.post("https://api.telegram.org/bottest-bot-token/sendMessage").mock(
        side_effect=[
            httpx.Response(400, json={"ok": False, "description": "can't parse entities"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = send_message(chat_id=42, text="**hi**")

    assert response.status_code == 200
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first["parse_mode"] == "HTML" and first["text"] == "<b>hi</b>"
    assert second == {"chat_id": 42, "text": "**hi**"}
