import asyncio
import dataclasses
import hmac
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import SYSTEM_PROMPT, AgentTurnResult, run_turn
from engine.core.guardrails import INJECTION_REFUSAL, filter_reply, is_injection_attempt, sanitize_user_text
from engine import contact_share, notifier, owner_bot, supabase_sync, sweeper
from engine import store as store_module
from engine.leads import extract_leads
from engine.mongo_client import load_history, log_turn
from engine.notifier import deliver_lead_alert
from engine.odoo_client import OdooClient
from engine.rate_limiter import FixedWindowRateLimiter
from engine.telegram_client import (
    extract_chat_id,
    extract_message,
    extract_sender,
    extract_shared_contact,
    extract_update_id,
    send_message,
)

logger = logging.getLogger(__name__)

HANDOFF_NOTE = "(passed to the team)"
HANDOFF_FAILED_REPLY = "Sorry, I couldn't pass that on just now. Please send it again in a moment."


def _secret_matches(given: str, expected: str) -> bool:
    """Constant-time, and safe for any header text: hmac.compare_digest raises on non-ASCII
    strings, which would turn a hostile header into a 500 instead of a refusal."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def owner_chat_id_problem(value) -> str | None:
    """Why TELEGRAM_ALERTS_CHAT_ID cannot work, or None. Buttons and replies are accepted only from
    the owner's private chat with the alert bot, whose id is a positive number."""
    text = str(value).strip() if value is not None else ""
    if not text.isdigit() or int(text) <= 0:
        return (
            "TELEGRAM_ALERTS_CHAT_ID must be the number of your private chat with the alert bot "
            "(a positive whole number, from getUpdates). Alerts may still arrive, but buttons and replies will be ignored."
        )
    return None

# How long an untouched lead waits before the owner is nudged. 0 turns the nudge off.
LEAD_REMINDER_MINUTES = int(os.environ.get("LEAD_REMINDER_MINUTES", "30") or 0)


@asynccontextmanager
async def lifespan(_app):
    """Start the background nudge for untouched leads, if alerts are set up."""
    if os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN"):
        problem = owner_chat_id_problem(os.environ.get("TELEGRAM_ALERTS_CHAT_ID"))
        if problem:
            logger.error(problem)
    task = None
    alerts_ready = os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN") and os.environ.get("TELEGRAM_ALERTS_CHAT_ID")
    if LEAD_REMINDER_MINUTES > 0 and alerts_ready:
        task = asyncio.create_task(sweeper.run_forever(_sweep_deps, LEAD_REMINDER_MINUTES))
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="LeadGate Engine", lifespan=lifespan)

config = load_config()

# Hot cache of conversation history, keyed by chat_id. MongoDB's
# `conversations` collection is the durable copy: after a restart (or once a
# chat has been evicted from this cache) the history is rebuilt from it on the
# chat's next message. The cache holds at most MAX_TRACKED_CHATS chats, least
# recently used first out, so memory stays bounded however many chats show up.
conversation_history: OrderedDict[int, list[dict]] = OrderedDict()
MAX_TRACKED_CHATS = 1000

# Recently seen Telegram update_ids. Telegram redelivers an update when it
# does not get a 200 in time, and a redelivery must not run the agent (and its
# lead-creating tool) a second time.
_seen_updates: OrderedDict[int, None] = OrderedDict()
SEEN_UPDATES_MAX = 2000
_seen_owner_updates: OrderedDict[int, None] = OrderedDict()

# Cap on how many user turns of history are kept per chat_id. Without a
# cap, conversation_history grows forever for any chat_id that keeps
# messaging, unlike the rate limiter (Task 6.1) which evicts old entries.
# Every message in history is resent to the LLM on every subsequent call,
# so unbounded growth means unbounded per-request token cost, not just
# unbounded memory. 20 turns is generous enough for a real back-and-forth
# sales conversation (the eval's longest real transcripts are far shorter)
# while keeping a hard ceiling on both memory and LLM spend per chat_id.
CONVERSATION_HISTORY_MAX_TURNS = 20

# Per-chat_id rate limit for the webhook: 10 requests per 60-second rolling
# window. Generous enough for a normal back-and-forth conversation but tight
# enough to stop a single chat from hammering the LLM/Odoo backends and
# running up API costs. In-memory only, matching conversation_history above
# - fine for a single-process portfolio deployment, not for multiple workers.
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limiter = FixedWindowRateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)

# Lead creation is the only tool that writes to Odoo, so it gets its own,
# much tighter limit: 3 leads per chat per hour. A real customer needs one.
WRITE_LIMIT_MAX = 3
WRITE_LIMIT_WINDOW_SECONDS = 3600
_write_limiter = FixedWindowRateLimiter(WRITE_LIMIT_MAX, WRITE_LIMIT_WINDOW_SECONDS)

# Maximum length (in characters) of incoming Telegram text accepted into the
# LLM conversation history. Long enough for genuine user messages, short
# enough to stop a pathological huge message from bloating LLM context/cost.
# Oversized messages are truncated (rather than rejected outright) so the
# conversation still proceeds with whatever the user actually meant to say.
MAX_MESSAGE_LENGTH = 2000

# What a customer sees if the assistant fails to produce an answer (the model
# provider erred, Odoo was unreachable). Silence would read as "the bot is dead".
ERROR_REPLY = "Sorry, I couldn't answer that just now. Please send it again in a moment."

# Lazily-constructed singletons. Built on first use (not at import time) so
# importing this module - e.g. under pytest - doesn't require a live Odoo
# connection or LLM API key just to collect tests. Tests patch these two
# factory functions directly to avoid real network calls.
_adapter = None
_llm_client = None


def _get_adapter():
    global _adapter
    if _adapter is None:
        odoo = OdooClient(
            config["ODOO_URL"], config["ODOO_DB"], config["ODOO_USER"], config["ODOO_PASSWORD"]
        )
        if config["ACTIVE_DOMAIN"] == "real_estate":
            _adapter = RealEstateAdapter(odoo)
        else:
            _adapter = CarsAdapter(odoo)
    return _adapter


def _get_llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = get_llm_client(config)
    return _llm_client


def _mirror_status(lead_id: int, status: str) -> None:
    try:
        supabase_sync.update_lead(lead_id, {"status": status})
    except Exception:
        logger.exception("could not mirror the status of lead %s", lead_id)


def _owner_deps() -> owner_bot.OwnerDeps:
    return owner_bot.OwnerDeps(
        odoo=_get_adapter().odoo,
        store=store_module.get_store(),
        owner=notifier,
        tell_customer=lambda chat_id, text: send_message(chat_id, text, strict=True),
        mirror_status=_mirror_status,
        owner_chat_id=int(os.environ["TELEGRAM_ALERTS_CHAT_ID"]),
    )


def _share_deps() -> contact_share.ShareDeps:
    return contact_share.ShareDeps(
        odoo=_get_adapter().odoo,
        store=store_module.get_store(),
        owner=notifier,
        send_customer=lambda chat_id, text, markup=None: send_message(chat_id, text, reply_markup=markup, strict=True),
        mirror_phone=lambda lead_id, phone: supabase_sync.update_lead(lead_id, {"phone": phone}),
    )


def _seen_before(update_id) -> bool:
    """Telegram redelivers an update it did not get a 200 for. Remember recent ids so a redelivery is ignored."""
    if update_id is None:
        return False
    if update_id in _seen_updates:
        return True
    _seen_updates[update_id] = None
    while len(_seen_updates) > SEEN_UPDATES_MAX:
        _seen_updates.popitem(last=False)
    return False


def _sweep_deps() -> sweeper.SweepDeps:
    import datetime as dt

    return sweeper.SweepDeps(
        odoo=_get_adapter().odoo,
        store=store_module.get_store(),
        owner=notifier,
        now=lambda: dt.datetime.now(dt.timezone.utc),
    )


def _clean_restored(messages: list[dict]) -> list[dict]:
    """Run stored turns through the same screening as live ones. Turns logged
    before the guardrails existed were never sanitised, and an old injection
    attempt must not steer the chat again just because it came back from
    MongoDB."""
    cleaned: list[dict] = []
    for user, assistant in zip(messages[0::2], messages[1::2]):
        text = sanitize_user_text(user["content"])
        if not text or is_injection_attempt(text):
            continue
        cleaned.append({"role": "user", "content": text})
        cleaned.append({"role": "assistant", "content": filter_reply(assistant["content"], SYSTEM_PROMPT)})
    return cleaned


def _is_private(chat_id, sender: dict | None) -> bool:
    """In a private chat the chat id is the user's own id. A group is never asked for, or believed
    about, a phone number: anyone in it could answer."""
    return bool(sender) and sender.get("id") == chat_id


def _handle_new_leads(chat_id: int, result, sender: dict | None = None) -> None:
    """Alert the owner and mirror each lead created this turn to the dashboard.

    The lead already exists in Odoo, so nothing here may fail the request: each
    step is isolated and only logged if it goes wrong.
    """
    try:
        leads = extract_leads(result, config["ACTIVE_DOMAIN"])
    except Exception:
        logger.exception("could not read leads from the turn for chat_id=%s", chat_id)
        return
    username = (sender or {}).get("username")
    for lead in leads:
        lead = dataclasses.replace(lead, username=username)
        # Remember which chat the lead came from, so the owner can answer the customer.
        can_reply = False
        try:
            store_module.get_store().save_lead_chat(
                lead.lead_id, chat_id=chat_id, kind=lead.kind, item_name=lead.item_name,
                customer_name=lead.customer_name, detail=lead.detail, username=username,
            )
            can_reply = True
        except Exception:
            logger.exception("could not remember the chat for lead %s", lead.lead_id)
        try:
            delivery = deliver_lead_alert(lead, can_reply=can_reply)
            if can_reply and delivery.message_id:
                store_module.get_store().add_alert_message(lead.lead_id, delivery.message_id)
        except Exception:
            logger.exception("lead alert failed for lead %s", lead.lead_id)
        try:
            supabase_sync.record_lead(chat_id, lead)
        except Exception:
            logger.exception("could not mirror lead %s to Supabase", lead.lead_id)
        # A customer with no public username cannot be opened from the alert, so ask them for a number.
        try:
            if _is_private(chat_id, sender):  # a share-my-number button only works in a private chat
                contact_share.maybe_ask_for_number(_share_deps(), chat_id, lead, username)
        except Exception:
            logger.exception("could not ask chat_id=%s for a phone number", chat_id)


def _mirror_turn(chat_id: int, message: str, reply: str, tool_calls: list, blocked: bool = False) -> None:
    try:
        supabase_sync.record_turn(chat_id, config["ACTIVE_DOMAIN"], message, reply, tool_calls, blocked=blocked)
    except Exception:
        logger.exception("could not mirror the turn to Supabase for chat_id=%s", chat_id)


def _after_shared_contact(chat_id: int, sender_id, contact: dict) -> None:
    try:
        contact_share.handle_shared_contact(_share_deps(), chat_id, sender_id, contact)
    except Exception:
        logger.exception("could not handle a shared contact for chat_id=%s", chat_id)


def _after_typed_phone(chat_id: int, text: str) -> None:
    try:
        contact_share.maybe_attach_typed_phone(_share_deps(), chat_id, text)
    except Exception:
        logger.exception("could not use a typed phone number for chat_id=%s", chat_id)


def _after_reply(chat_id: int, text: str, result, mongodb_uri: str | None, delivered: bool, sender: dict | None = None) -> None:
    """Everything that happens after the customer has their reply: tell the owner
    about any lead, then log the turn. Runs as a background task, so a slow Supabase
    or Telegram cannot hold up the next customer's message."""
    # The lead exists in Odoo whether or not the customer saw the reply, so the
    # owner is told either way.
    _handle_new_leads(chat_id, result, sender)
    if not delivered:
        return  # a turn the customer never saw is not logged

    # MongoDB's `conversations` collection is the audit log. A Mongo fault must
    # never matter to the customer, so it is only logged.
    if mongodb_uri:
        try:
            log_turn(mongodb_uri, chat_id, config["ACTIVE_DOMAIN"], text, result.reply, result.tool_calls_made)
        except Exception:
            logger.exception("log_turn failed for chat_id=%s", chat_id)
    _mirror_turn(chat_id, text, result.reply, result.tool_calls_made)


def _after_blocked(chat_id: int, text: str, mongodb_uri: str | None) -> None:
    if mongodb_uri:
        try:
            log_turn(mongodb_uri, chat_id, config["ACTIVE_DOMAIN"], text, INJECTION_REFUSAL, [], blocked=True)
        except Exception:
            logger.exception("log_turn failed for chat_id=%s", chat_id)
    _mirror_turn(chat_id, text, INJECTION_REFUSAL, [], blocked=True)


def _after_handoff(chat_id: int, text: str, handoff: dict, mongodb_uri: str | None) -> None:
    """The chat is in human mode: pass the customer's message to the owner and keep a record."""
    passed_on = False
    try:
        delivery = owner_bot.forward_customer_message(_owner_deps(), handoff, text)
        passed_on = bool(getattr(delivery, "ok", False))
    except Exception:
        logger.exception("could not forward a customer message to the owner for chat_id=%s", chat_id)
    if not passed_on:
        # Silence would look like a dead bot for hours, so say the message did not get through.
        try:
            send_message(chat_id, HANDOFF_FAILED_REPLY)
        except Exception:
            logger.exception("could not tell chat_id=%s that their message was not passed on", chat_id)
    if mongodb_uri:
        try:
            log_turn(mongodb_uri, chat_id, config["ACTIVE_DOMAIN"], text, HANDOFF_NOTE, [], handled_by="human")
        except Exception:
            logger.exception("log_turn failed for chat_id=%s", chat_id)
    _mirror_turn(chat_id, text, HANDOFF_NOTE, [])


def _trim_conversation_history(chat_id: int) -> None:
    """Keep only the last CONVERSATION_HISTORY_MAX_TURNS user turns (and
    their associated assistant/tool messages) for a given chat_id, always
    preserving the leading system-prompt message if present.
    """
    history = conversation_history.get(chat_id)
    if not history:
        return

    system_msgs = [m for m in history if m.get("role") == "system"]
    rest = [m for m in history if m.get("role") != "system"]

    user_indices = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    if len(user_indices) > CONVERSATION_HISTORY_MAX_TURNS:
        cutoff = user_indices[-CONVERSATION_HISTORY_MAX_TURNS]
        rest = rest[cutoff:]

    conversation_history[chat_id] = system_msgs + rest


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    # Verify the secret token before doing anything else, including
    # parsing the request body, so a malformed/malicious body can't cause
    # a crash before the auth check runs. Comparison is constant-time
    # (hmac.compare_digest) rather than `!=` so a network attacker timing
    # responses can't use early-exit string comparison to guess the secret
    # one character at a time.
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if not _secret_matches(secret_header, config["TELEGRAM_WEBHOOK_SECRET"]):
        return Response(status_code=401)

    body = await request.json()

    shared = extract_shared_contact(body)
    if shared is not None:
        # The customer pressed the share-my-number button. It is not a message for the assistant.
        contact_chat, contact_sender = extract_chat_id(body), extract_sender(body)
        if contact_chat is None or contact_sender is None or not _is_private(contact_chat, contact_sender):
            return {"ok": True}
        if not _rate_limiter.allow(contact_chat):
            return Response(status_code=429)
        if _seen_before(extract_update_id(body)):
            return {"ok": True}
        background_tasks.add_task(_after_shared_contact, contact_chat, contact_sender["id"], shared)
        return {"ok": True}

    extracted = extract_message(body)
    if extracted is None:
        # Non-text update (e.g. edited message, channel post, callback
        # query) - nothing to reply to, acknowledge and move on.
        return {"ok": True}

    chat_id, text = extracted

    if not _rate_limiter.allow(chat_id):
        return Response(status_code=429)

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    if _seen_before(extract_update_id(body)):
        return {"ok": True}

    text = sanitize_user_text(text)
    if not text:
        return {"ok": True}

    sender = extract_sender(body)
    if _is_private(chat_id, sender) and contact_share.looks_like_phone(text):
        # Someone we asked for a number may have typed it. Checked off the request path.
        background_tasks.add_task(_after_typed_phone, chat_id, text)

    mongodb_uri = os.environ.get("MONGODB_URI")

    # A person has taken over this chat: the assistant stays quiet and the owner reads it.
    try:
        handoff = await run_in_threadpool(store_module.get_store().get_handoff, chat_id)
    except Exception:
        logger.exception("could not check human mode for chat_id=%s", chat_id)
        handoff = None
    if handoff:
        background_tasks.add_task(_after_handoff, chat_id, text, handoff, mongodb_uri)
        return {"ok": True}

    if is_injection_attempt(text):
        # Refuse without spending an LLM call, touching Odoo, or letting the
        # message into the conversation history where it could keep steering
        # later turns.
        try:
            send_message(chat_id, INJECTION_REFUSAL)
        except Exception:
            logger.exception("send_message failed for chat_id=%s", chat_id)
        background_tasks.add_task(_after_blocked, chat_id, text, mongodb_uri)
        return {"ok": True}

    if chat_id in conversation_history:
        conversation_history.move_to_end(chat_id)
    else:
        restored: list[dict] = []
        if mongodb_uri:
            try:
                restored = _clean_restored(
                    load_history(mongodb_uri, chat_id, CONVERSATION_HISTORY_MAX_TURNS)
                )
            except Exception:
                logger.exception("load_history failed for chat_id=%s", chat_id)
        conversation_history[chat_id] = restored
        while len(conversation_history) > MAX_TRACKED_CHATS:
            conversation_history.popitem(last=False)
    history = conversation_history[chat_id]
    # run_turn inserts the system prompt at index 0, so rolling back by index
    # would be off by one. Restore a copy of the whole list instead.
    history_before_turn = list(history)
    history.append({"role": "user", "content": text})

    # Everything below this point talks to Odoo, the LLM provider, Telegram,
    # and MongoDB -- any of which can fault (Odoo down, LLM timeout/429,
    # Telegram API error, Mongo unreachable). None of those should crash this
    # process or return a 500: Telegram retries a failed webhook delivery by
    # re-sending the *same* update, which would re-run this whole handler
    # (re-billing the LLM call and, worse, potentially re-executing a tool
    # call that already wrote a crm.lead, if the fault happened after tool
    # execution but before the reply was sent). Returning 200 here stops that
    # retry storm. A redelivery that still arrives (the 200 got lost in
    # transit) is recognised by its `update_id` above and ignored.
    tool_events: list = []
    try:
        result = run_turn(
            history,
            _get_adapter(),
            _get_llm_client(),
            chat_id=chat_id,
            write_limiter=_write_limiter,
            tool_events=tool_events,
        )
    except Exception:
        logger.exception("run_turn failed for chat_id=%s", chat_id)
        # Drop the half-finished turn (the user message, the system prompt if
        # run_turn just added it, and any tool messages) so the customer's
        # retry doesn't stack a duplicate.
        history[:] = history_before_turn
        try:
            send_message(chat_id, ERROR_REPLY)
        except Exception:
            logger.exception("could not send the error reply to chat_id=%s", chat_id)
        # A lead may already exist in Odoo even though the reply step failed. The owner
        # still needs to hear about it; a resend by the customer gets the same lead back.
        if tool_events:
            background_tasks.add_task(
                _handle_new_leads,
                chat_id,
                AgentTurnResult(
                    reply="",
                    tool_calls_made=[call for call, _ in tool_events],
                    tool_results=[outcome for _, outcome in tool_events],
                ),
                sender,
            )
        return {"ok": True}

    _trim_conversation_history(chat_id)

    delivered = True
    try:
        send_message(chat_id, result.reply)
    except Exception:
        logger.exception("send_message failed for chat_id=%s", chat_id)
        delivered = False

    background_tasks.add_task(_after_reply, chat_id, text, result, mongodb_uri, delivered, sender)
    return {"ok": True}


@app.post("/webhook/alerts")
async def alerts_webhook(request: Request):
    """Updates from the alert bot: the owner pressing a button or replying to an alert.

    Fails closed: with no secret configured the route refuses everything, and the secret
    is compared in constant time. Beyond that, owner_bot ignores any update that does not
    come from the owner's own chat."""
    expected = os.environ.get("TELEGRAM_ALERTS_WEBHOOK_SECRET")
    if not expected:
        return Response(status_code=503)
    if not _secret_matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "", expected):
        return Response(status_code=401)

    body = await request.json()
    update_id = extract_update_id(body) if isinstance(body, dict) else None
    if update_id is not None:
        if update_id in _seen_owner_updates:
            return {"ok": True}
        _seen_owner_updates[update_id] = None
        while len(_seen_owner_updates) > SEEN_UPDATES_MAX:
            _seen_owner_updates.popitem(last=False)
    try:
        await run_in_threadpool(owner_bot.handle_update, body, _owner_deps())
    except Exception:
        logger.exception("could not handle an alert-bot update")
    return {"ok": True}
