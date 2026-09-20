"""The owner's side of the conversation, through the alert bot.

The owner gets an alert for each lead with buttons under it, and can answer a customer by
replying to that alert. Replying puts the chat in human mode: the assistant stays quiet and
the customer's next messages are forwarded to the owner, until the owner hands the chat back
or the time runs out.

Nothing a customer writes can reach this code: it is fed only by the alert bot's own webhook,
which checks a secret, and every update is ignored unless it comes from the owner's chat.
Every dependency is passed in (OwnerDeps), so the whole thing can be tested without Telegram,
Odoo or a database.

Buttons stay on screen for ever, a finger can slip, and Telegram can deliver twice, so every
action looks at where the lead stands first: a lead that is won or archived is left alone, a
stage is never moved backwards, and a viewing is confirmed once.
"""
import datetime as dt
import html
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.core import followup
from engine.core.actions import holder_key
from engine.notifier import ACTIONS
from engine.sweeper import tag_as_reminded

logger = logging.getLogger(__name__)

HANDOFF_HOURS = 6
RELAY_MAX = 1000
FORWARD_MAX = 600  # Telegram refuses messages over 4096 characters and escaping can make text five times longer

HELP = (
    "Tap the buttons under an alert to act on a lead. Reply to an alert to write to that "
    "customer. The assistant then stays quiet in that chat.\n\n"
    "/open lists leads still waiting for you\n"
    "/back <lead number> hands a chat back to the assistant\n"
    "/help shows this"
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class OwnerDeps:
    odoo: Any
    store: Any
    owner: Any  # what the alert bot offers: send_owner_message, send_owner_html, answer_callback
    tell_customer: Callable[[int, str], None]  # sends through the customer bot
    mirror_status: Callable[[int, str], None]  # updates the dashboard's copy of the lead
    owner_chat_id: int
    now: Callable[[], dt.datetime] = field(default=_utcnow)


# ---------------------------------------------------------------- entry point

def handle_update(update: dict, deps: OwnerDeps) -> None:
    """Handle one update from the alert bot. Never raises."""
    try:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            if _is_owner(callback.get("from"), (callback.get("message") or {}).get("chat"), deps):
                _handle_callback(callback, deps)
            return
        message = update.get("message")
        if isinstance(message, dict) and _is_owner(message.get("from"), message.get("chat"), deps):
            _handle_message(message, deps)
    except Exception:
        logger.exception("could not handle an owner update")


def _is_owner(sender, chat, deps: OwnerDeps) -> bool:
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return False
    return sender.get("id") == deps.owner_chat_id and chat.get("id") == deps.owner_chat_id


# ---------------------------------------------------------------- where a lead stands

def _lead_state(deps: OwnerDeps, lead_id: int) -> dict | None:
    """The lead as Odoo has it, archived or not, or None if it does not exist."""
    rows = deps.odoo.search_read(
        "crm.lead",
        [("id", "=", lead_id), "|", ("active", "=", True), ("active", "=", False)],
        ["stage_id", "active", "probability", "email_from", "phone"],
    )
    return rows[0] if rows else None


def _stage_name(state: dict) -> str:
    stage = state.get("stage_id")
    return stage[1] if isinstance(stage, (list, tuple)) and len(stage) > 1 else ""


def _open_lead(deps: OwnerDeps, lead_id: int) -> tuple[dict | None, str | None]:
    """(state, None) if the lead can still be acted on, otherwise (None, what to tell the owner)."""
    state = _lead_state(deps, lead_id)
    if state is None:
        return None, "That lead no longer exists."
    if not state.get("active"):
        return None, "That lead is already closed."
    if _stage_name(state) == "Won":
        return None, "That lead is already won and closed."
    return state, None


def _advance_from_new(deps: OwnerDeps, lead_id: int, state: dict | None) -> None:
    """Move a brand-new lead to Qualified. A lead already further along is left where it is."""
    if state is not None and _stage_name(state) == "New":
        followup.set_stage(deps.odoo, lead_id, "Qualified")


# ---------------------------------------------------------------- buttons

def _handle_callback(callback: dict, deps: OwnerDeps) -> None:
    callback_id = str(callback.get("id") or "")
    code, _, rest = str(callback.get("data") or "").partition(":")
    if code not in ACTIONS or not rest.isdigit():
        deps.owner.answer_callback(callback_id, "That button is out of date.")
        return
    lead_id = int(rest)
    action = _ACTIONS.get(code)
    try:
        toast = action(deps, lead_id)
    except Exception:
        logger.exception("button %s failed for lead %s", code, lead_id)
        toast = "Something went wrong. It is in the log."
    deps.owner.answer_callback(callback_id, toast)


def _held_item(deps: OwnerDeps, lead_id: int, state: dict | None = None) -> dict | None:
    """The item held for this lead. Found by the lead's number; if that link is missing, by
    the customer holding it, so a half-recorded hold can still be sold or released."""
    rows = deps.odoo.search_read(
        "leadgate.catalog.item",
        [("reservation_lead_id", "=", lead_id), ("status", "=", "reserved")],
        ["id", "name"],
    )
    if rows:
        return rows[0]
    holders = [key for key in (holder_key(state.get("email_from")), holder_key(state.get("phone"))) if key] if state else []
    if not holders:
        return None
    rows = deps.odoo.search_read(
        "leadgate.catalog.item",
        [("reserved_for", "in", holders), ("status", "=", "reserved")],
        ["id", "name"],
    )
    return rows[0] if rows else None


def _release_hold(deps: OwnerDeps, lead_id: int, state: dict | None) -> bool:
    item = _held_item(deps, lead_id, state)
    if item is None:
        return False
    deps.odoo.call("leadgate.catalog.item", "action_release", [[item["id"]]], {})
    return True


def _take(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    owner_id = followup.salesperson_id(deps.odoo)
    if owner_id:
        deps.odoo.write("crm.lead", [lead_id], {"user_id": owner_id})
    tag_as_reminded(deps.odoo, lead_id)  # the owner has it, so the reminder is not needed
    followup.post_note(deps.odoo, lead_id, "Picked up from Telegram.")
    deps.mirror_status(lead_id, "taken")
    return "It is yours."


def _contacted(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    followup.complete_activities(deps.odoo, lead_id, "Contacted the customer")
    _advance_from_new(deps, lead_id, state)
    followup.post_note(deps.odoo, lead_id, "Marked as contacted from Telegram.")
    deps.mirror_status(lead_id, "contacted")
    return "Marked as contacted."


def _lost(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    _release_hold(deps, lead_id, state)  # a lost reservation must not keep the item off the board
    followup.complete_activities(deps.odoo, lead_id, "Closed as lost")
    followup.mark_lost(deps.odoo, lead_id)
    deps.mirror_status(lead_id, "lost")
    return "Marked as lost."


def _sold(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    item = _held_item(deps, lead_id, state)
    if item is None:
        return "No held item found for this lead."
    deps.odoo.write("leadgate.catalog.item", [item["id"]], {"status": "sold"})
    followup.complete_activities(deps.odoo, lead_id, "Sold")
    followup.set_stage(deps.odoo, lead_id, "Won")
    followup.post_note(deps.odoo, lead_id, "Marked as sold from Telegram.")
    deps.mirror_status(lead_id, "won")
    return "Sold. The key leaves the board."


def _release(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    released = _release_hold(deps, lead_id, state)
    followup.complete_activities(deps.odoo, lead_id, "Hold released")
    followup.mark_lost(deps.odoo, lead_id)
    deps.mirror_status(lead_id, "released")
    return "Hold released." if released else "There was no active hold. Lead closed."


def _confirm_viewing(deps: OwnerDeps, lead_id: int) -> str:
    state, refused = _open_lead(deps, lead_id)
    if refused:
        return refused
    if deps.store.has_flag(lead_id, "viewing_confirmed"):
        return "Already confirmed."
    chat = deps.store.get_lead_chat(lead_id)
    _advance_from_new(deps, lead_id, state)
    followup.post_note(deps.odoo, lead_id, "Viewing confirmed from Telegram.")
    deps.mirror_status(lead_id, "confirmed")
    if not chat:
        return "Confirmed in Odoo, but I cannot message the customer (chat not found)."
    when = chat.get("detail") or "the requested time"
    deps.tell_customer(
        chat["chat_id"],
        f"Your viewing of {chat.get('item_name') or 'the item'} is confirmed for {when}. We look forward to seeing you.",
    )
    deps.store.set_flag(lead_id, "viewing_confirmed")  # only once the customer has been told
    return "Confirmed. The customer was told."


def _open_reply_box(deps: OwnerDeps, lead_id: int) -> str:
    chat = deps.store.get_lead_chat(lead_id)
    if not chat:
        return "I do not have this customer's chat, so I cannot send a message."
    who = chat.get("customer_name") or "the customer"
    delivery = deps.owner.send_owner_message(
        f"Reply to this message to write to {who} about {chat.get('item_name') or 'their request'}.",
        force_reply=True,
    )
    if delivery.message_id:
        deps.store.add_alert_message(lead_id, delivery.message_id)
    return "Type your message as a reply."


def _hand_back(deps: OwnerDeps, lead_id: int) -> str:
    chat = deps.store.get_lead_chat(lead_id)
    if chat:
        deps.store.clear_handoff(chat["chat_id"])
    who = (chat or {}).get("customer_name") or f"lead {lead_id}"
    deps.owner.send_owner_message(f"{who} is back with the assistant.")
    return "Handed back."


_ACTIONS = {
    "t": _take,
    "c": _contacted,
    "l": _lost,
    "s": _sold,
    "x": _release,
    "v": _confirm_viewing,
    "r": _open_reply_box,
    "b": _hand_back,
}


# ---------------------------------------------------------------- messages

def _handle_message(message: dict, deps: OwnerDeps) -> None:
    text = message.get("text")
    if not isinstance(text, str):
        return
    text = text.strip()
    if not text:
        return
    if text.startswith("/"):
        _handle_command(text, deps)
        return
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    if replied_to is None:
        deps.owner.send_owner_message("Reply to one of my alerts to write to that customer, or send /help.")
        return
    _relay(text, replied_to, deps)


def _relay(text: str, replied_to: int, deps: OwnerDeps) -> None:
    lead_id = deps.store.lead_for_message(replied_to)
    if lead_id is None:
        deps.owner.send_owner_message("I cannot tell which customer that is for. Reply to one of the lead alerts.")
        return
    chat = deps.store.get_lead_chat(lead_id)
    if not chat:
        deps.owner.send_owner_message("I do not have that customer's chat any more, so I cannot send this.")
        return
    try:
        deps.tell_customer(chat["chat_id"], text[:RELAY_MAX])
    except Exception:
        logger.exception("could not relay the owner's reply for lead %s", lead_id)
        deps.owner.send_owner_message("Telegram did not accept the message. Nothing was sent.")
        return

    # The customer has the message, so the assistant must stop answering over the owner. That
    # comes first; the Odoo bookkeeping below can fail without leaving the chat in a mess.
    deps.store.set_handoff(chat["chat_id"], lead_id, until=deps.now() + dt.timedelta(hours=HANDOFF_HOURS))
    try:
        state = _lead_state(deps, lead_id)
        followup.post_note(deps.odoo, lead_id, f"Owner replied via Telegram: {text}")
        if state is not None and state.get("active") and _stage_name(state) != "Won":
            followup.complete_activities(deps.odoo, lead_id, "Replied to the customer")
            _advance_from_new(deps, lead_id, state)
            deps.mirror_status(lead_id, "contacted")
    except Exception:
        logger.exception("the reply was sent but Odoo could not be updated for lead %s", lead_id)

    who = chat.get("customer_name") or "the customer"
    deps.owner.send_owner_message(
        f"Sent to {who}. The assistant stays quiet in that chat for {HANDOFF_HOURS} hours. "
        f"Send /back {lead_id} to hand it back sooner."
    )


def _handle_command(text: str, deps: OwnerDeps) -> None:
    command, _, argument = text.partition(" ")
    command = command.split("@")[0].lower()
    if command in ("/start", "/help"):
        deps.owner.send_owner_message(HELP)
    elif command == "/open":
        _list_open(deps)
    elif command == "/back":
        if argument.strip().isdigit():
            _hand_back(deps, int(argument.strip()))
        else:
            deps.owner.send_owner_message("Send /back followed by the lead number, for example /back 71.")
    else:
        deps.owner.send_owner_message(HELP)


def _age(created: str | None, now: dt.datetime) -> str:
    try:
        started = dt.datetime.strptime(created or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return "a while"
    minutes = max(0, int((now - started).total_seconds() // 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _list_open(deps: OwnerDeps) -> None:
    rows = deps.odoo.search_read(
        "crm.lead",
        [("stage_id.name", "=", "New")],
        ["name", "contact_name", "create_date"],
        order="create_date asc",
        limit=8,
    )
    if not rows:
        deps.owner.send_owner_message("Nothing is waiting for you.")
        return
    now = deps.now()
    esc = lambda value: html.escape(str(value or ""), quote=False)
    lines = ["<b>Waiting for you</b>"]
    for row in rows:
        lines.append(f"#{row['id']} {esc(row.get('contact_name')) or 'Unnamed'}: {esc(row.get('name'))} ({_age(row.get('create_date'), now)})")
    deps.owner.send_owner_html("\n".join(lines))


# ---------------------------------------------------------------- human mode

def forward_customer_message(deps: OwnerDeps, handoff: dict, text: str):
    """While a chat is in human mode, pass the customer's message to the owner instead of the
    assistant. The text is the customer's, so it is escaped. Returns the Delivery, so the caller
    can tell the customer if it could not be passed on."""
    lead_id = handoff["lead_id"]
    chat = deps.store.get_lead_chat(lead_id) or {}
    who = html.escape(chat.get("customer_name") or "Customer", quote=False)
    body = f"<b>{who}</b> (lead #{lead_id}):\n{html.escape(text[:FORWARD_MAX], quote=False)}"
    buttons = {
        "inline_keyboard": [
            [
                {"text": ACTIONS["r"], "callback_data": f"r:{lead_id}"},
                {"text": ACTIONS["b"], "callback_data": f"b:{lead_id}"},
            ]
        ]
    }
    delivery = deps.owner.send_owner_html(body, buttons=buttons)
    if delivery.message_id:
        deps.store.add_alert_message(lead_id, delivery.message_id)
    return delivery
