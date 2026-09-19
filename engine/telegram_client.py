import html
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_BASE = "https://api.telegram.org"


_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BULLET = re.compile(r"^[-*] (?=\S)", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6} +(.+)$", re.MULTILINE)


def format_for_telegram(text: str) -> str:
    """Turn the model's Markdown into the small HTML subset Telegram renders.

    Sent as plain text, `**2020 Camry**` reaches the customer with its asterisks.
    The text is HTML-escaped first, so nothing a customer or the catalog wrote can
    become markup; only the bold, bullet and heading patterns below are converted.
    """
    escaped = html.escape(text, quote=False)
    escaped = _HEADING.sub(r"<b>\1</b>", escaped)
    escaped = _BULLET.sub("• ", escaped)
    return _BOLD.sub(r"<b>\1</b>", escaped)


def send_message(chat_id: int, text: str) -> httpx.Response:
    """Send a message to a Telegram chat via the Bot API, with light formatting.

    If Telegram rejects the formatted version, the original text goes out as plain
    text so the customer still gets a reply.
    """
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    response = httpx.post(
        url,
        json={"chat_id": chat_id, "text": format_for_telegram(text), "parse_mode": "HTML"},
        timeout=10,
    )
    if response.status_code == 400:
        response = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    return response


def extract_message(update: dict) -> tuple[int, str] | None:
    """Pull (chat_id, text) out of a Telegram update payload.

    Returns None for updates that don't contain a text message (e.g. edited
    messages, channel posts, callback queries, or a message with no text),
    instead of raising, so the webhook handler never crashes on odd payloads.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or "id" not in chat or not isinstance(text, str):
        return None

    return chat["id"], text


def extract_update_id(update: dict) -> int | None:
    """Telegram's per-bot update counter, used to recognise redelivered updates."""
    update_id = update.get("update_id")
    return update_id if isinstance(update_id, int) and not isinstance(update_id, bool) else None
