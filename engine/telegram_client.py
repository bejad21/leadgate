import os

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_message(chat_id: int, text: str) -> httpx.Response:
    """Send a plain-text message to a Telegram chat via the Bot API."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    return httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)


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
