import html
import os
import re

import httpx
from dotenv import load_dotenv

from engine.notifier import valid_username

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


def send_message(chat_id: int, text: str, reply_markup: dict | None = None, *, strict: bool = False) -> httpx.Response:
    """Send a message to a Telegram chat via the Bot API, with light formatting.

    `reply_markup` is an optional keyboard (for example the share-my-number button, or a request to
    remove it). If Telegram rejects the formatted version, the original text goes out as plain
    text, with the same keyboard, so the customer still gets a reply. With `strict`, a rejection
    that survives that retry (the customer blocked the bot, say) raises instead of being returned.
    """
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": format_for_telegram(text), "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = httpx.post(url, json=payload, timeout=10)
    if response.status_code == 400:
        plain = {"chat_id": chat_id, "text": text}
        if reply_markup:
            plain["reply_markup"] = reply_markup
        response = httpx.post(url, json=plain, timeout=10)
    if strict:
        response.raise_for_status()
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


def extract_chat_id(update: dict) -> int | None:
    message = update.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    return chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None


def extract_sender(update: dict) -> dict | None:
    """Who wrote the message: their id, their public username (only if it is a valid Telegram
    username, so a hostile one never becomes a link) and first name. None if there is no sender."""
    message = update.get("message")
    sender = message.get("from") if isinstance(message, dict) else None
    if not isinstance(sender, dict):
        return None
    sender_id = sender.get("id")
    if not isinstance(sender_id, int) or isinstance(sender_id, bool):
        return None
    username = sender.get("username")
    first_name = sender.get("first_name")
    return {
        "id": sender_id,
        "username": username if valid_username(username) else None,
        "first_name": first_name if isinstance(first_name, str) else None,
    }


def extract_shared_contact(update: dict) -> dict | None:
    """The contact card in a message (what the share-my-number button sends), or None."""
    message = update.get("message")
    contact = message.get("contact") if isinstance(message, dict) else None
    if not isinstance(contact, dict) or not isinstance(contact.get("phone_number"), str):
        return None
    first_name = contact.get("first_name")
    return {
        "phone_number": contact["phone_number"],
        "first_name": first_name if isinstance(first_name, str) else None,
        "user_id": contact.get("user_id"),
    }
