"""Getting a phone number from a customer who has no public Telegram username.

A customer with a public username can be opened straight from the alert. One without cannot, so
the bot asks them once, with Telegram's share-my-number button (or they can just type it). What
comes back is attached to their lead in Odoo and the dashboard, and the owner is alerted with the
number, a WhatsApp link and a Talk button.

Two rules keep this honest:
- A shared contact counts only if Telegram says it is the sender's own (contact.user_id equals
  the sender's id). Anyone can forward someone else's contact card.
- A typed number is only used after the bot has asked for one, and it must be long enough to be a
  real number, so a price like "1 200 000" is never taken for a phone.

Every dependency is passed in (ShareDeps), so it is tested without Telegram, Odoo or a database.
"""
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from engine.core import followup
from engine.notifier import valid_username

logger = logging.getLogger(__name__)

PROMPT = (
    "If you'd like our team to reach you directly, tap the button below to share your phone number, "
    "or just type it here."
)
CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}

THANKS = "Thank you, we've got your number. Our team can reach you directly now."
NOT_YOURS = "Please share your own number with the button, so we know it is yours."
NOT_A_NUMBER = "That doesn't look like a phone number. Please share it with the button, or type it with the country code."
NO_LEAD = "Thank you. We'll use it if you decide to enquire about something."

# A number typed in a message: at least nine digits, written the way a phone number is (a plus, a
# leading zero or the country code), so a price, a date or an order number is never mistaken for one.
_TYPED = re.compile(r"\+?\d[\d\s().\-]{7,20}\d")
_ONLY_PHONE_CHARS = re.compile(r"\+?[\d\s().\-]+")
TYPED_MIN_DIGITS = 9


@dataclass
class ShareDeps:
    odoo: Any
    store: Any
    owner: Any  # deliver_number_alert
    send_customer: Callable[..., None]  # (chat_id, text, markup=None), through the customer bot
    mirror_phone: Callable[[int, str], None]  # the dashboard's copy of the lead


def _country_code() -> str:
    return os.environ.get("DEFAULT_PHONE_COUNTRY_CODE", "971")


def normalise_phone(raw) -> str | None:
    """A phone number in international form (+971501234567), or None if it is not one.

    Telegram sends a shared contact as bare digits with the country code; a customer types a
    local number with a leading zero. A leading zero gets the shop's country code
    (DEFAULT_PHONE_COUNTRY_CODE, 971 by default). Seven to fifteen digits, as in E.164."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or not _ONLY_PHONE_CHARS.fullmatch(text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    elif digits.startswith("0"):
        number = _country_code() + digits.lstrip("0")
    else:
        number = digits
    return f"+{number}" if 7 <= len(number) <= 15 else None


def looks_like_phone(text: str) -> bool:
    """A cheap check, so the store is only consulted for messages that might hold a number."""
    return isinstance(text, str) and _TYPED.search(text) is not None


def _typed_number(text: str) -> str | None:
    for match in _TYPED.finditer(text or ""):
        raw = match.group().strip()
        digits = re.sub(r"\D", "", raw)
        if not (raw.startswith("+") or digits.startswith("0") or digits.startswith(_country_code())):
            continue  # a bare run of digits, such as a date or an order number
        number = normalise_phone(raw)
        if number and len(re.sub(r"\D", "", number)) >= TYPED_MIN_DIGITS:
            return number
    return None


# ---------------------------------------------------------------- asking

def maybe_ask_for_number(deps: ShareDeps, chat_id: int, lead, username) -> bool:
    """Ask a customer for a number, once, if there is no username to open them by and no number
    on the lead. Returns whether the question went out."""
    if valid_username(username) or lead.phone:
        return False
    if inherit_known_number(deps, chat_id, lead):
        return False  # the customer shared a number on an earlier lead
    if not deps.store.claim_chat_flag(chat_id, "phone_asked"):
        return False  # claimed before sending, so two leads at once send one question
    try:
        deps.send_customer(chat_id, PROMPT, CONTACT_KEYBOARD)
    except Exception:
        logger.exception("could not ask chat_id=%s for a phone number", chat_id)
        deps.store.clear_chat_flag(chat_id, "phone_asked")  # not asked after all, so the next lead tries again
        return False
    return True


def inherit_known_number(deps: ShareDeps, chat_id: int, lead) -> str | None:
    """A customer who already shared a number, on an earlier lead, does not need to share it again.
    Give it to this lead too. Returns the number, or None if there was nothing to give."""
    if lead.phone:
        return None
    phone = deps.store.phone_for_chat(chat_id)
    if not phone:
        return None
    try:
        deps.odoo.write("crm.lead", [lead.lead_id], {"phone": phone})
    except Exception:
        logger.exception("could not copy the number to lead %s in Odoo", lead.lead_id)
        return None
    deps.store.set_lead_phone(lead.lead_id, phone)
    try:
        deps.mirror_phone(lead.lead_id, phone)
    except Exception:
        logger.exception("could not mirror the number of lead %s to the dashboard", lead.lead_id)
    return phone


# ---------------------------------------------------------------- receiving

def handle_shared_contact(deps: ShareDeps, chat_id: int, sender_id, contact: dict) -> None:
    """The customer pressed the share button (or sent a contact card)."""
    user_id = contact.get("user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id != sender_id:
        _tell(deps, chat_id, NOT_YOURS, CONTACT_KEYBOARD)
        return
    phone = normalise_phone(contact.get("phone_number"))
    if phone is None:
        _tell(deps, chat_id, NOT_A_NUMBER, CONTACT_KEYBOARD)
        return
    _use_number(deps, chat_id, phone)


def maybe_attach_typed_phone(deps: ShareDeps, chat_id: int, text: str) -> bool:
    """A customer typed a number after being asked for one. Returns whether it was used."""
    if not deps.store.has_chat_flag(chat_id, "phone_asked"):
        return False
    phone = _typed_number(text)
    if phone is None:
        return False
    chat = deps.store.latest_lead_for_chat(chat_id)
    if not chat or chat.get("phone"):
        return False
    _use_number(deps, chat_id, phone)
    return True


def _tell(deps: ShareDeps, chat_id: int, text: str, markup=None) -> None:
    try:
        deps.send_customer(chat_id, text, markup)
    except Exception:
        logger.exception("could not answer chat_id=%s", chat_id)


def _give_partner_the_number(deps: ShareDeps, partner, phone: str) -> None:
    """Put the number on the contact, but never over one the contact already has."""
    if not (isinstance(partner, (list, tuple)) and partner):
        return
    rows = deps.odoo.search_read("res.partner", [("id", "=", partner[0])], ["phone"])
    if rows and not rows[0].get("phone"):
        deps.odoo.write("res.partner", [partner[0]], {"phone": phone})


def _use_number(deps: ShareDeps, chat_id: int, phone: str) -> None:
    chat = deps.store.latest_lead_for_chat(chat_id)
    if not chat:
        _tell(deps, chat_id, NO_LEAD, REMOVE_KEYBOARD)
        return
    if chat.get("phone"):
        _tell(deps, chat_id, THANKS, REMOVE_KEYBOARD)
        return
    lead_id = chat["lead_id"]

    state = None
    try:
        rows = deps.odoo.search_read(
            "crm.lead", [("id", "=", lead_id), "|", ("active", "=", True), ("active", "=", False)], ["phone", "partner_id"]
        )
        state = rows[0] if rows else None
    except Exception:
        logger.exception("could not read lead %s before adding a number", lead_id)
    if state and state.get("phone"):
        deps.store.set_lead_phone(lead_id, state["phone"])  # Odoo already has one; leave it alone
        _tell(deps, chat_id, THANKS, REMOVE_KEYBOARD)
        return

    saved = False
    try:
        deps.odoo.write("crm.lead", [lead_id], {"phone": phone})
        saved = True
        _give_partner_the_number(deps, (state or {}).get("partner_id"), phone)
        followup.post_note(deps.odoo, lead_id, f"The customer shared their phone number on Telegram: {phone}")
    except Exception:
        logger.exception("could not finish adding the number to lead %s in Odoo", lead_id)
    if saved:
        # Only what Odoo accepted is remembered, so a number that failed to save can be sent again.
        deps.store.set_lead_phone(lead_id, phone)
        try:
            deps.mirror_phone(lead_id, phone)
        except Exception:
            logger.exception("could not mirror the number of lead %s to the dashboard", lead_id)

    try:
        delivery = deps.owner.deliver_number_alert(lead_id, chat.get("customer_name") or "", phone, chat.get("username"), can_talk=True)
        if getattr(delivery, "message_id", None):
            deps.store.add_alert_message(lead_id, delivery.message_id)
    except Exception:
        logger.exception("could not alert the owner about the number for lead %s", lead_id)
    _tell(deps, chat_id, THANKS, REMOVE_KEYBOARD)
