"""Talk to the owner through a separate Telegram bot.

The alert bot is not the customer bot: it has its own token, it only ever messages the
owner's chat, and customers cannot see it. Configure it with TELEGRAM_ALERTS_BOT_TOKEN and
TELEGRAM_ALERTS_CHAT_ID. If either is missing everything here is skipped, so the engine
runs the same without it.

Everything is best-effort. An alert that fails must never break the customer's reply or
lose the lead, so every failure is logged and reported as a failed Delivery, never raised.
"""
import html
import logging
import os
import re
from typing import NamedTuple

import httpx

from engine.leads import LeadInfo

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 5

HEADINGS = {"lead": "New lead", "reservation": "New reservation", "viewing": "New viewing request"}


# Telegram usernames are 5 to 32 letters, digits or underscores, starting with a letter. Anything
# else is never turned into a link, so a hostile "username" cannot point the owner somewhere else.
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")


def valid_username(name) -> bool:
    return isinstance(name, str) and _USERNAME.fullmatch(name) is not None


def chat_link(username: str) -> str:
    return f"https://t.me/{username}"


def whatsapp_link(phone: str) -> str:
    return f"https://wa.me/{re.sub(r'[^0-9]', '', phone)}"


class Delivery(NamedTuple):
    ok: bool
    message_id: int | None = None


def _lead_url(lead_id: int) -> str:
    base = (os.environ.get("ODOO_PUBLIC_URL") or os.environ.get("ODOO_URL") or "").rstrip("/")
    return f"{base}/odoo/action-crm.crm_lead_all_leads/{lead_id}"


def format_alert(lead: LeadInfo) -> str:
    """The alert text, in Telegram's HTML subset. Everything a customer typed is
    escaped, so a name like "<b>Boss</b>" arrives as text, not markup."""
    esc = html.escape
    heading = HEADINGS.get(lead.kind, HEADINGS["lead"])
    lines = [f"<b>{heading}</b> #{lead.lead_id}", f"<b>{esc(lead.item_name, quote=False)}</b>"]
    if lead.detail:
        lines.append(esc(lead.detail, quote=False))

    who = esc(lead.customer_name, quote=False) or "Unnamed customer"
    contact = " · ".join(esc(part, quote=False) for part in (lead.email, lead.phone) if part)
    lines.append(f"{who}")
    lines.append(contact or "No contact given")
    if valid_username(lead.username):
        lines.append(f"Telegram: @{lead.username}")

    if lead.price is not None:
        if lead.price_verified:
            lines.append(f"${lead.price:,.0f}")
        else:
            lines.append(f"Price not verified (customer said ${lead.price:,.0f})")

    url = esc(_lead_url(lead.lead_id), quote=True)
    lines.append(f'<a href="{url}">Open in Odoo</a>')
    return "\n".join(lines)


# ---- buttons -----------------------------------------------------------------------
# callback_data is "<action>:<lead id>". Telegram allows 64 bytes, so a short code is used.
ACTIONS = {
    "t": "Take it",
    "c": "Contacted",
    "r": "Reply",
    "l": "Lost",
    "s": "Mark sold",
    "x": "Release hold",
    "v": "Confirm viewing",
    "b": "Hand back to bot",
    "k": "Talk here",
}


def _button(code: str, lead_id: int) -> dict:
    return {"text": ACTIONS[code], "callback_data": f"{code}:{lead_id}"}


def alert_keyboard(lead: LeadInfo, can_reply: bool = True) -> dict:
    """The buttons under an alert. What can be done depends on what the lead is."""
    if lead.kind == "reservation":
        rows = [["s", "x"], ["c"]]
    elif lead.kind == "viewing":
        rows = [["v", "c"], ["l"]]
    else:
        rows = [["t", "c"], ["l"]]
    if can_reply:
        rows[-1].insert(0, "r")
    keyboard = [[_button(code, lead.lead_id) for code in row] for row in rows]
    extras = []
    if can_reply:
        extras.append(_button("k", lead.lead_id))  # talk to this customer without replying each time
    if valid_username(lead.username):
        extras.append({"text": "Open chat", "url": chat_link(lead.username)})
    if extras:
        keyboard.append(extras)
    return {"inline_keyboard": keyboard}


def format_number_alert(lead_id: int, name: str, phone: str, username: str | None) -> str:
    esc = html.escape
    lines = [f"<b>{esc(name, quote=False) or 'A customer'}</b> shared a phone number", f"Lead #{lead_id}", esc(phone, quote=False)]
    if valid_username(username):
        lines.append(f"Telegram: @{username}")
    return "\n".join(lines)


def number_keyboard(lead_id: int, phone: str, username: str | None, can_talk: bool) -> dict:
    """Buttons for a customer who shared a number: talk here, open WhatsApp, open their Telegram profile."""
    row = []
    if can_talk:
        row.append(_button("k", lead_id))
    row.append({"text": "WhatsApp", "url": whatsapp_link(phone)})
    if valid_username(username):
        row.append({"text": "Open chat", "url": chat_link(username)})
    return {"inline_keyboard": [row]}


# ---- calls to the alert bot ----------------------------------------------------------

def _credentials() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_ALERTS_CHAT_ID")
    return (token, chat_id) if token and chat_id else None


def _post(method: str, payload: dict) -> httpx.Response | None:
    creds = _credentials()
    if creds is None:
        return None
    try:
        return httpx.post(f"{TELEGRAM_API_BASE}/bot{creds[0]}/{method}", json=payload, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        logger.exception("could not reach Telegram (%s)", method)
        return None


def _send(text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> Delivery:
    creds = _credentials()
    if creds is None:
        logger.debug("alert bot not configured; skipping a message to the owner")
        return Delivery(False)
    payload = {"chat_id": creds[1], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    response = _post("sendMessage", payload)
    if response is None:
        return Delivery(False)
    if response.status_code != 200:
        logger.error("the owner message was rejected by Telegram: HTTP %s", response.status_code)
        return Delivery(False)
    try:
        message_id = response.json().get("result", {}).get("message_id")
    except ValueError:
        message_id = None
    return Delivery(True, message_id if isinstance(message_id, int) else None)


def deliver_lead_alert(lead: LeadInfo, can_reply: bool = True) -> Delivery:
    """Send the alert with its buttons and report the message id, so a reply to it can be traced."""
    return _send(format_alert(lead), alert_keyboard(lead, can_reply))


def deliver_number_alert(lead_id: int, name: str, phone: str, username: str | None, can_talk: bool = True) -> Delivery:
    """Tell the owner that a customer shared a number, with a way to talk to them."""
    return _send(format_number_alert(lead_id, name, phone, username), number_keyboard(lead_id, phone, username, can_talk))


def send_lead_alert(lead: LeadInfo) -> bool:
    return deliver_lead_alert(lead).ok


def send_owner_message(text: str, *, buttons: dict | None = None, force_reply: bool = False, reply_to: int | None = None) -> Delivery:
    """A message to the owner. `text` is plain and is escaped here; `force_reply` makes
    Telegram open the reply box so the owner's answer is tied to this message."""
    markup = {"force_reply": True} if force_reply else buttons
    return _send(html.escape(text, quote=False), markup, reply_to)


def send_owner_html(text: str, *, buttons: dict | None = None) -> Delivery:
    """Like send_owner_message, for text the caller has already built and escaped."""
    return _send(text, buttons)


def answer_callback(callback_id: str, text: str = "") -> None:
    """Every button press must be answered, or the owner's app shows a spinner."""
    _post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
