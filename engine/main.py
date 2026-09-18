import hmac
import logging
import os

from fastapi import FastAPI, Request, Response

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import run_turn
from engine.mongo_client import log_turn
from engine.odoo_client import OdooClient
from engine.rate_limiter import FixedWindowRateLimiter
from engine.telegram_client import extract_message, send_message

logger = logging.getLogger(__name__)

app = FastAPI(title="LeadGate Engine")

config = load_config()

# In-memory conversation history, keyed by chat_id. Acceptable for a
# portfolio project; a production deployment would move this to a
# persistent store such as Redis or Supabase so history survives restarts
# and is shared across worker processes.
conversation_history: dict[int, list[dict]] = {}

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

# Maximum length (in characters) of incoming Telegram text accepted into the
# LLM conversation history. Long enough for genuine user messages, short
# enough to stop a pathological huge message from bloating LLM context/cost.
# Oversized messages are truncated (rather than rejected outright) so the
# conversation still proceeds with whatever the user actually meant to say.
MAX_MESSAGE_LENGTH = 2000

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
async def telegram_webhook(request: Request):
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

    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})

    # Everything below this point talks to Odoo, the LLM provider, Telegram,
    # and MongoDB -- any of which can fault (Odoo down, LLM timeout/429,
    # Telegram API error, Mongo unreachable). None of those should crash this
    # process or return a 500: Telegram retries a failed webhook delivery by
    # re-sending the *same* update, which would re-run this whole handler
    # (re-billing the LLM call and, worse, potentially re-executing a tool
    # call that already wrote a crm.lead, if the fault happened after tool
    # execution but before the reply was sent). Returning 200 here stops that
    # retry storm. This is NOT full exactly-once delivery: it does not
    # deduplicate by Telegram's `update_id`, so a retry that arrives despite
    # a *successful* prior 200 (e.g. the 200 itself got lost in transit)
    # could still double-process. That's a known, real follow-up, not
    # something silently assumed solved here.
    try:
        result = run_turn(history, _get_adapter(), _get_llm_client())
    except Exception:
        logger.exception("run_turn failed for chat_id=%s", chat_id)
        return {"ok": True}

    _trim_conversation_history(chat_id)

    try:
        send_message(chat_id, result.reply)
    except Exception:
        logger.exception("send_message failed for chat_id=%s", chat_id)
        return {"ok": True}

    # Log the completed turn to MongoDB's `conversations` collection
    # (Task 3.4). This is best-effort audit logging: a Mongo fault must
    # never take down the webhook after a real reply has already been
    # computed and sent to the customer.
    mongodb_uri = os.environ.get("MONGODB_URI")
    if mongodb_uri:
        try:
            log_turn(
                mongodb_uri,
                chat_id,
                config["ACTIVE_DOMAIN"],
                text,
                result.reply,
                result.tool_calls_made,
            )
        except Exception:
            logger.exception("log_turn failed for chat_id=%s", chat_id)

    return {"ok": True}
