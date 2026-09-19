import hmac
import logging
import os
from collections import OrderedDict

from fastapi import BackgroundTasks, FastAPI, Request, Response

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import SYSTEM_PROMPT, AgentTurnResult, run_turn
from engine.core.guardrails import INJECTION_REFUSAL, filter_reply, is_injection_attempt, sanitize_user_text
from engine import supabase_sync
from engine.leads import extract_leads
from engine.mongo_client import load_history, log_turn
from engine.notifier import send_lead_alert
from engine.odoo_client import OdooClient
from engine.rate_limiter import FixedWindowRateLimiter
from engine.telegram_client import extract_message, extract_update_id, send_message

logger = logging.getLogger(__name__)

app = FastAPI(title="LeadGate Engine")

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


def _handle_new_leads(chat_id: int, result) -> None:
    """Alert the owner and mirror each lead created this turn to the dashboard.

    The lead already exists in Odoo, so nothing here may fail the request: each
    step is isolated and only logged if it goes wrong.
    """
    try:
        leads = extract_leads(result, config["ACTIVE_DOMAIN"])
    except Exception:
        logger.exception("could not read leads from the turn for chat_id=%s", chat_id)
        return
    for lead in leads:
        try:
            send_lead_alert(lead)
        except Exception:
            logger.exception("lead alert failed for lead %s", lead.lead_id)
        try:
            supabase_sync.record_lead(chat_id, lead)
        except Exception:
            logger.exception("could not mirror lead %s to Supabase", lead.lead_id)


def _mirror_turn(chat_id: int, message: str, reply: str, tool_calls: list, blocked: bool = False) -> None:
    try:
        supabase_sync.record_turn(chat_id, config["ACTIVE_DOMAIN"], message, reply, tool_calls, blocked=blocked)
    except Exception:
        logger.exception("could not mirror the turn to Supabase for chat_id=%s", chat_id)


def _after_reply(chat_id: int, text: str, result, mongodb_uri: str | None, delivered: bool) -> None:
    """Everything that happens after the customer has their reply: tell the owner
    about any lead, then log the turn. Runs as a background task, so a slow Supabase
    or Telegram cannot hold up the next customer's message."""
    # The lead exists in Odoo whether or not the customer saw the reply, so the
    # owner is told either way.
    _handle_new_leads(chat_id, result)
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
    if not hmac.compare_digest(secret_header, config["TELEGRAM_WEBHOOK_SECRET"]):
        return Response(status_code=401)

    body = await request.json()
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

    update_id = extract_update_id(body)
    if update_id is not None:
        if update_id in _seen_updates:
            return {"ok": True}
        _seen_updates[update_id] = None
        while len(_seen_updates) > SEEN_UPDATES_MAX:
            _seen_updates.popitem(last=False)

    text = sanitize_user_text(text)
    if not text:
        return {"ok": True}

    mongodb_uri = os.environ.get("MONGODB_URI")

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
            )
        return {"ok": True}

    _trim_conversation_history(chat_id)

    delivered = True
    try:
        send_message(chat_id, result.reply)
    except Exception:
        logger.exception("send_message failed for chat_id=%s", chat_id)
        delivered = False

    background_tasks.add_task(_after_reply, chat_id, text, result, mongodb_uri, delivered)
    return {"ok": True}
