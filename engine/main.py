from fastapi import FastAPI, Request, Response

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import run_turn
from engine.odoo_client import OdooClient
from engine.rate_limiter import FixedWindowRateLimiter
from engine.telegram_client import extract_message, send_message

app = FastAPI(title="LeadGate Engine")

config = load_config()

# In-memory conversation history, keyed by chat_id. Acceptable for a
# portfolio project; a production deployment would move this to a
# persistent store such as Redis or Supabase so history survives restarts
# and is shared across worker processes.
conversation_history: dict[int, list[dict]] = {}

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    # Verify the secret token before doing anything else, including
    # parsing the request body, so a malformed/malicious body can't cause
    # a crash before the auth check runs.
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != config["TELEGRAM_WEBHOOK_SECRET"]:
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

    result = run_turn(history, _get_adapter(), _get_llm_client())
    send_message(chat_id, result.reply)

    return {"ok": True}
