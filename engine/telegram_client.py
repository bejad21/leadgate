import html
import math
import os
import re
import time

import httpx
from dotenv import load_dotenv

from engine.notifier import valid_username

load_dotenv()

TELEGRAM_API_BASE = "https://api.telegram.org"

# Longest a send waits when Telegram says "slow down" (429). A longer wait would hold the customer's
# request, so past this the message is reported as not delivered.
MAX_RETRY_AFTER_SECONDS = 5


# A preview would fetch and show whatever a link points at; replies are meant to carry no links anyway.
NO_LINK_PREVIEW = {"is_disabled": True}


class TelegramSendError(RuntimeError):
    """A send Telegram refused. The message carries the method and status only: the request URL
    contains the bot token, so it must never end up in an exception text or a log."""

    def __init__(self, method: str, status_code: int):
        super().__init__(f"Telegram {method} failed: HTTP {status_code}")
        self.status_code = status_code


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


def _post(url: str, payload: dict) -> httpx.Response:
    """POST, and on a 429 wait the `retry_after` Telegram asks for (capped) and try once more."""
    response = httpx.post(url, json=payload, timeout=10)
    if response.status_code == 429:
        try:
            wait = float(response.json()["parameters"]["retry_after"])
        except (ValueError, KeyError, TypeError, OverflowError):
            return response
        if not math.isfinite(wait) or wait < 0:
            return response  # nonsense from Telegram: report "not delivered" rather than guess a wait
        time.sleep(min(wait, MAX_RETRY_AFTER_SECONDS))
        response = httpx.post(url, json=payload, timeout=10)
    return response


def send_message(chat_id: int, text: str, *, strict: bool = False, reply_markup: dict | None = None) -> httpx.Response | None:
    """Send a message to a Telegram chat via the Bot API, with light formatting.

    Returns the response if Telegram accepted it and None if it did not (the customer blocked the
    bot, a server fault, a rate limit that outlasted one short retry). If Telegram rejects the
    formatted version, the original text goes out as plain text, so the customer still gets a
    reply. With `strict`, a failed send raises TelegramSendError instead
    of returning None. `reply_markup` is an inline keyboard shown under the message.
    """
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": format_for_telegram(text),
        "parse_mode": "HTML",
        "link_preview_options": NO_LINK_PREVIEW,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = _post(url, payload)
    if response.status_code == 400:
        plain = {"chat_id": chat_id, "text": text, "link_preview_options": NO_LINK_PREVIEW}
        if reply_markup:
            plain["reply_markup"] = reply_markup
        response = _post(url, plain)
    if response.is_success:
        return response
    if strict:
        raise TelegramSendError("sendMessage", response.status_code) from None
    return None


def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    """Tell Telegram a button tap was received, which stops the button's loading spinner. Best effort: a tap that cannot be
    answered is not worth failing the request for."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        return _post(f"{TELEGRAM_API_BASE}/bot{bot_token}/answerCallbackQuery", payload).is_success
    except httpx.HTTPError:
        return False


def clear_keyboard(chat_id: int, message_id: int) -> bool:
    """Remove the buttons from a message, so the same tap cannot be made twice. Best effort."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
    try:
        return _post(f"{TELEGRAM_API_BASE}/bot{bot_token}/editMessageReplyMarkup", payload).is_success
    except httpx.HTTPError:
        return False


def extract_callback(update: dict) -> dict | None:
    """The button tap in an update: {id, chat_id, message_id, data, sender}, or None when the update is not a tap with
    usable fields."""
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    message = callback.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    callback_id, data = callback.get("id"), callback.get("data")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not isinstance(callback_id, str) or not isinstance(data, str):
        return None
    message_id = message.get("message_id") if isinstance(message, dict) else None
    return {
        "id": callback_id,
        "chat_id": chat_id,
        "message_id": message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
        "data": data,
        "sender": extract_sender({"message": {"from": callback.get("from")}}),
    }


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
