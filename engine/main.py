import asyncio
import dataclasses
import datetime as dt
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import cast

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from engine import buttons, logging_setup, notifier, odoo_status, owner_bot, supabase_sync, tracing
from engine import store as store_module
from engine.adapters import ADAPTERS
from engine.config import LLM_KEY_VARS, configured_llm_models, get_llm_client, load_config, webhook_secret_problem
from engine.core.actions import VIEWING_WINDOW_DAYS
from engine.core.agent_loop import PROMPT_VERSION, SYSTEM_PROMPT, AgentTurnResult, run_turn
from engine.core.guardrails import (
    INJECTION_REFUSAL,
    filter_reply,
    is_injection_attempt,
    sanitize_tool_result,
    sanitize_user_text,
)
from engine.core.paging import OFFSET_ARGUMENT, PAGE_SIZE, latest_search, next_page, search_key
from engine.core.tools import parse_tool_args
from engine.leads import extract_leads
from engine.llm_client import Sampling
from engine.notifier import deliver_lead_alert
from engine.odoo_client import OdooClient, OdooLoginError
from engine.rate_limiter import StoreRateLimiter
from engine.telegram_client import (
    answer_callback_query,
    clear_keyboard,
    extract_callback,
    extract_message,
    extract_sender,
    extract_update_id,
    send_message,
)

logger = logging.getLogger(__name__)


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


@asynccontextmanager
async def lifespan(_app):
    """Set up logging and refuse bad settings before the first message arrives."""
    Sampling.from_env()  # a bad LLM_* value stops the engine here, not on the first customer message
    logging_setup.configure_logging()
    tracing.check_trace_file()
    store_module.text_retention_days()  # a bad deletion period stops the engine here
    store = store_module.get_store()  # a bad SUPABASE_URL or encryption key stops the engine here
    if _workers() > 1 and isinstance(store, store_module.InMemoryStore):
        raise RuntimeError(
            "WEB_CONCURRENCY is above 1 but SUPABASE_URL and SUPABASE_SERVICE_KEY are not set. Workers share chats "
            "through the store; run one worker without it."
        )
    if os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN"):
        problem = owner_chat_id_problem(os.environ.get("TELEGRAM_ALERTS_CHAT_ID"))
        if problem:
            logger.error(problem)
    status_problem = odoo_status.secret_problem(os.environ.get("ODOO_WEBHOOK_SECRET"))
    if status_problem:
        logger.warning(status_problem)
    purge_task = asyncio.create_task(_purge_forever())
    yield
    purge_task.cancel()


# How often the store drops what it no longer needs (old update ids, finished rate-limit windows, expired lead chats).
PURGE_INTERVAL_SECONDS = 6 * 3600


async def _purge_forever(interval: float = PURGE_INTERVAL_SECONDS) -> None:
    """Purge once at start-up and then every `interval` seconds, until cancelled. A store that is down is skipped."""
    while True:
        try:
            await asyncio.to_thread(store_module.get_store().purge_expired)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("could not purge the store (%s)", type(exc).__name__)
        await asyncio.sleep(interval)


# The tunnel makes every route public, so the interactive API pages are off.
app = FastAPI(title="LeadGate Engine", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

config = load_config()

# Hot cache of conversation history by chat_id. The store's chat_turns is the durable copy and
# rebuilds a chat's history on its next message. Past MAX_TRACKED_CHATS the least recently used goes.
conversation_history: OrderedDict[int, list[dict]] = OrderedDict()
MAX_TRACKED_CHATS = 1000

# Recently seen update_ids, kept in memory as a fast first check. Telegram redelivers an update it
# didn't get a 200 for, and a redelivery must not run the agent and its lead-creating tool again. The
# store's claim_update is the durable second check: it also catches a redelivery after a restart.
_seen_updates: OrderedDict[int, None] = OrderedDict()
SEEN_UPDATES_MAX = 2000
_seen_owner_updates: OrderedDict[int, None] = OrderedDict()
_seen_lock = threading.Lock()

# One turn at a time per chat: the history list and the rollback in the webhook assume it. Inside one process an
# asyncio lock orders the turns; across workers a claim in the store does. Entries exist only while a chat has a turn
# running or waiting, so the registry cannot outgrow the traffic.
_chat_locks: dict[int, list] = {}
_WORKER_ID = uuid.uuid4().hex[:12]
# A claim lasts this long and is renewed while the turn runs, so a worker that dies frees its chats soon.
CHAT_LEASE_SECONDS = 120
# The longest a turn waits for another worker before it goes ahead anyway: silence would read as "the bot is dead".
CHAT_LEASE_WAIT_SECONDS = 150
CHAT_LEASE_POLL_SECONDS = 0.25
# Chats whose last state could not be saved: the copy in memory is newer than the stored one until a save works.
_unsaved_chats: set[int] = set()


def _workers() -> int:
    raw = (os.environ.get("WEB_CONCURRENCY") or "1").strip()
    return int(raw) if raw.isdigit() else 1


@asynccontextmanager
async def _chat_turn(chat_id: int):
    entry = _chat_locks.get(chat_id)
    if entry is None:
        entry = _chat_locks[chat_id] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            async with _chat_lease(chat_id):
                yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            del _chat_locks[chat_id]


@asynccontextmanager
async def _chat_lease(chat_id: int):
    """Hold the store's claim on this chat for the turn. If the store cannot be asked, the turn runs without it."""
    owner = f"{_WORKER_ID}:{uuid.uuid4().hex[:8]}"
    store = store_module.get_store()

    async def claim() -> bool:
        return await run_in_threadpool(store.claim_chat, chat_id, owner, CHAT_LEASE_SECONDS)

    async def keep() -> None:
        while True:
            await asyncio.sleep(CHAT_LEASE_SECONDS / 4)
            try:
                await claim()
            except Exception as exc:
                logger.warning("could not renew the claim on a chat (%s)", type(exc).__name__)

    held = False
    deadline = time.monotonic() + CHAT_LEASE_WAIT_SECONDS
    try:
        while not (held := await claim()):
            if time.monotonic() >= deadline:
                logger.warning("another worker still holds this chat after %ss; answering anyway", CHAT_LEASE_WAIT_SECONDS)
                break
            await asyncio.sleep(CHAT_LEASE_POLL_SECONDS)
    except Exception as exc:
        logger.warning("could not claim a chat in the store (%s); answering without the claim", type(exc).__name__)
    renewal = asyncio.create_task(keep()) if held else None
    try:
        yield
    finally:
        if renewal:
            renewal.cancel()
        if held:
            try:
                await run_in_threadpool(store.release_chat, chat_id, owner)
            except Exception as exc:
                logger.warning("could not release the claim on a chat (%s)", type(exc).__name__)


# User turns of history kept per chat_id. The whole history is resent to the LLM on every call, so
# an unbounded history means unbounded token cost as well as memory.
CONVERSATION_HISTORY_MAX_TURNS = 20

# Per-chat_id webhook limit, so one chat can't hammer the LLM and Odoo. Counted in the store, so it
# holds across restarts and workers; if the store is down it is counted in memory.
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limiter = StoreRateLimiter("webhook", RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS, store_module.get_store)

# State-changing tools (lead, hold, viewing) get a tighter limit: 3 per chat per hour.
WRITE_LIMIT_MAX = 3
WRITE_LIMIT_WINDOW_SECONDS = 3600
_write_limiter = StoreRateLimiter("write", WRITE_LIMIT_MAX, WRITE_LIMIT_WINDOW_SECONDS, store_module.get_store)

# Longest incoming text kept in the conversation. Longer messages are truncated, not rejected, so
# the conversation still proceeds.
MAX_MESSAGE_LENGTH = 2000

# What a customer sees if the assistant fails to produce an answer (the model
# provider erred, Odoo was unreachable). Silence would read as "the bot is dead".
ERROR_REPLY = "Sorry, I couldn't answer that just now. Please send it again in a moment."

# Built on first use, not at import, so importing this module (for example under pytest) needs no
# live Odoo connection or LLM key. Tests patch the two factory functions directly.
_adapter = None
_llm_client = None


# The first call logs in to Odoo. Handlers run it on a worker thread, and the lock keeps two
# threads from each building (and logging in) their own adapter.
_adapter_lock = threading.Lock()


# A failed Odoo login is remembered for this long: callers get the same error at once instead of each
# waiting out its own login (up to ODOO_TIMEOUT_SECONDS) while holding the lock. A later success clears it.
ADAPTER_FAILURE_COOLDOWN_SECONDS = 10.0
_adapter_failure: tuple[float, Exception] | None = None
_adapter_clock = time.monotonic


def _get_adapter():
    global _adapter, _adapter_failure
    if _adapter is None:
        with _adapter_lock:
            if _adapter is None:
                failure = _adapter_failure
                if failure is not None and _adapter_clock() - failure[0] < ADAPTER_FAILURE_COOLDOWN_SECONDS:
                    raise failure[1].with_traceback(None)
                try:
                    odoo = OdooClient(
                        config["ODOO_URL"], config["ODOO_DB"], config["ODOO_USER"], config["ODOO_PASSWORD"]
                    )
                    # load_config has already refused an ACTIVE_DOMAIN that is not in the registry.
                    _adapter = ADAPTERS[config["ACTIVE_DOMAIN"]](odoo)
                except Exception as exc:
                    _adapter_failure = (_adapter_clock(), exc)
                    raise
                _adapter_failure = None
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


def _is_blocked(chat_id: int) -> bool:
    """True when the owner has blocked this chat. A store that cannot answer blocks nobody."""
    try:
        return store_module.get_store().is_blocked(chat_id) is True
    except Exception:
        logger.exception("could not check the block list")
        return False


def _seen_before(update_id, source: str = "customer", seen: OrderedDict | None = None) -> bool:
    """True if this update was already handled. Runs on a worker thread: the store call is a network round trip."""
    if update_id is None:
        return False
    recent = _seen_updates if seen is None else seen
    with _seen_lock:  # check-and-add must be one step: two deliveries of an update can arrive on two threads
        if update_id in recent:
            return True
        recent[update_id] = None
        while len(recent) > SEEN_UPDATES_MAX:
            recent.popitem(last=False)
    try:
        return not store_module.get_store().claim_update(source, update_id)
    except Exception:
        logger.exception("could not check the update id in the store; relying on memory only")
        return False


def _clean_restored(messages: list[dict]) -> list[dict]:
    """Re-screen stored turns like live ones, so an injection attempt logged earlier cannot steer the chat after a restart."""
    cleaned: list[dict] = []
    for user, assistant in zip(messages[0::2], messages[1::2], strict=False):
        text = sanitize_user_text(user["content"])
        if not text or is_injection_attempt(text):
            continue
        cleaned.append({"role": "user", "content": text})
        cleaned.append({"role": "assistant", "content": filter_reply(assistant["content"], SYSTEM_PROMPT)})
    return cleaned


def _handle_new_leads(chat_id: int, result, sender: dict | None = None) -> None:
    """Alert the owner and mirror each lead created this turn to the dashboard.

    The lead already exists in Odoo, so nothing here may fail the request: each
    step is isolated and only logged if it goes wrong.
    """
    try:
        leads = extract_leads(result, config["ACTIVE_DOMAIN"])
    except Exception:
        logger.exception("could not read leads from the turn")
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


def _mirror_turn(chat_id: int, message: str, reply: str, tool_calls: list, blocked: bool = False) -> None:
    try:
        supabase_sync.record_turn(chat_id, config["ACTIVE_DOMAIN"], message, reply, tool_calls, blocked=blocked)
    except Exception:
        logger.exception("could not mirror the turn to Supabase")


def _log_turn(chat_id: int, text: str, reply: str, tool_calls: list, blocked: bool = False) -> None:
    """Keep the turn in the store, which is what rebuilds the chat after a restart. A store fault must
    never matter to the customer, so it is only logged."""
    try:
        store_module.get_store().log_turn(chat_id, config["ACTIVE_DOMAIN"], text, reply, tool_calls, blocked=blocked)
    except Exception:
        logger.exception("could not keep the turn in the store")


def _after_reply(chat_id: int, text: str, result, delivered: bool, sender: dict | None = None) -> None:
    """Everything that happens after the customer has their reply: tell the owner
    about any lead, then log the turn. Runs as a background task, so a slow Supabase
    or Telegram cannot hold up the next customer's message."""
    # The lead exists in Odoo whether or not the customer saw the reply, so the
    # owner is told either way.
    _handle_new_leads(chat_id, result, sender)
    if not delivered:
        return  # a turn the customer never saw is not logged

    _log_turn(chat_id, text, result.reply, result.tool_calls_made)
    _mirror_turn(chat_id, text, result.reply, result.tool_calls_made)


def _after_blocked(chat_id: int, text: str) -> None:
    _log_turn(chat_id, text, INJECTION_REFUSAL, [], blocked=True)
    _mirror_turn(chat_id, text, INJECTION_REFUSAL, [], blocked=True)


def _finish_trace(background_tasks: BackgroundTasks, trace: tracing.TurnTrace, outcome: str, blocked_reason: str | None = None) -> None:
    """Complete the turn's trace and write it once the response has gone out."""
    trace.outcome = outcome
    trace.blocked_reason = blocked_reason
    background_tasks.add_task(tracing.emit, trace)


def _trim_conversation_history(chat_id: int) -> None:
    """Keep only the last CONVERSATION_HISTORY_MAX_TURNS user turns with their assistant and tool messages, always keeping a leading system prompt."""
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
async def health() -> dict:  # async: a sync route would queue behind the worker threads running turns
    return {"status": "ok"}


# Each readiness check gets this long. A check that is still running afterwards is reported as timed out.
READY_CHECK_TIMEOUT_SECONDS = 3.0
# Probes run here, not in the pool that serves turns, so a hung Odoo cannot use up the threads customers need.
_ready_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ready")


class ReadinessProblem(Exception):
    """A readiness failure whose message is safe to show: it names the problem and nothing else."""


def _check_odoo() -> None:
    """Odoo answers an authenticated call. The adapter is built (and logged in) once and reused, so a
    probe costs one cheap read, not a login; if Odoo was down at start-up the next probe tries again."""
    odoo = _get_adapter().odoo
    odoo.search_read("res.users", [["id", "=", odoo.uid]], ["id"], limit=1)


def _check_store() -> None:
    store_module.get_store().ping()


def _check_llm() -> None:
    """Configuration only: a key and at least one model. The model itself is never called."""
    if not any(config.get(name) for name in LLM_KEY_VARS):
        raise ReadinessProblem("no LLM API key configured")
    if not configured_llm_models(config):
        raise ReadinessProblem("no model configured")


def _reason(exc: Exception) -> str:
    """A short, fixed-vocabulary reason. The exception's own text can hold a URL, a host or a
    password, so it is never passed on."""
    if isinstance(exc, ReadinessProblem):
        return str(exc)
    if isinstance(exc, OdooLoginError):
        return "login rejected"
    if isinstance(exc, OSError):  # refused, reset, DNS, socket timeout
        return "unreachable"
    return f"failed ({type(exc).__name__})"


async def _run_check(check) -> dict:
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(_ready_executor, check), READY_CHECK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": f"timed out after {READY_CHECK_TIMEOUT_SECONDS:g}s"}
    except Exception as exc:
        logger.warning("readiness check %s failed: %s", check.__name__, type(exc).__name__)
        return {"ok": False, "reason": _reason(exc)}
    return {"ok": True}


@app.get("/ready")
async def ready() -> Response:
    """Readiness, as opposed to /health's liveness: can this engine do its job right now? Checked on
    every request (nothing is cached, so a recovered dependency shows up at once)."""
    names = {"odoo": _check_odoo, "store": _check_store, "llm": _check_llm}
    outcomes = await asyncio.gather(*(_run_check(check) for check in names.values()))
    checks = dict(zip(names, outcomes, strict=True))
    is_ready = all(check["ok"] for check in checks.values())
    body = {"status": "ready" if is_ready else "not_ready", "checks": checks}
    return JSONResponse(body, status_code=200 if is_ready else 503)


async def _json_object(request: Request) -> dict | None:
    """The request body as a JSON object, or None when it is not one. Telegram retries a 500 but not a 400, so a
    body that cannot be read is refused instead of crashing the handler."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# Said when a button no longer fits the catalog or the calendar: the list it came from is old.
TAP_OUT_OF_DATE = "That button is out of date. Tell me what you are looking for and I will search again."
TAP_ITEM_GONE = "Sorry, that one is no longer available. Tell me what you are looking for and I will search again."
NO_MORE_MATCHES = "That was the last of them. Tell me if you want to change the search."


def _saved_state(chat_id: int) -> list[dict] | None:
    """The history another turn saved for this chat, search results included, or None when there is none."""
    try:
        state = store_module.get_store().load_state(chat_id)
        messages = json.loads(state) if state else None
    except Exception:
        logger.exception("could not read the chat's saved state")
        return None
    if not isinstance(messages, list) or not all(isinstance(m, dict) and "role" in m for m in messages):
        return None
    return [m for m in messages if m["role"] != "system"]  # the next turn adds today's prompt


async def _save_chat(chat_id: int) -> None:
    """Keep the chat's history in the store, where the next turn reads it, in this worker or another."""
    history = conversation_history.get(chat_id)
    if history is None:
        return
    state = json.dumps([m for m in history if m.get("role") != "system"], default=str)
    try:
        await run_in_threadpool(store_module.get_store().save_state, chat_id, state)
    except Exception:
        logger.exception("could not save the chat's state")
        _unsaved_chats.add(chat_id)
        return
    _unsaved_chats.discard(chat_id)


async def _history_for(chat_id: int) -> list[dict]:
    """The chat's history: what was saved last, else the copy in memory, else the stored turns (text only)."""
    saved = None if chat_id in _unsaved_chats else await run_in_threadpool(_saved_state, chat_id)
    if saved is not None:
        conversation_history[chat_id] = saved
    if chat_id in conversation_history:
        conversation_history.move_to_end(chat_id)
    else:
        restored: list[dict] = []
        try:
            restored = _clean_restored(
                await run_in_threadpool(store_module.get_store().load_history, chat_id, CONVERSATION_HISTORY_MAX_TURNS)
            )
        except Exception:
            logger.exception("could not load the chat's history from the store; starting it empty")
        conversation_history[chat_id] = restored
    while len(conversation_history) > MAX_TRACKED_CHATS:
        conversation_history.popitem(last=False)
    return conversation_history[chat_id]


async def _send(chat_id: int, text: str, markup: dict | None = None) -> bool:
    """Send a message, with buttons under it when there are any. True if Telegram took it."""
    try:
        if markup:
            return bool(await run_in_threadpool(lambda: send_message(chat_id, text, reply_markup=markup)))
        return bool(await run_in_threadpool(send_message, chat_id, text))
    except Exception:
        logger.exception("send_message failed")
        return False


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    # Check the secret token before parsing the body, so a malformed body can't crash before auth.
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if not _secret_matches(secret_header, config["TELEGRAM_WEBHOOK_SECRET"]):
        return Response(status_code=401)

    body = await _json_object(request)
    if body is None:
        return Response(status_code=400)

    extracted = extract_message(body)
    if extracted is None:
        tap = extract_callback(body)
        if tap is not None:
            return await _handle_tap(tap, extract_update_id(body), background_tasks)
        # Anything else (edited message, channel post, a message with no text): nothing to reply to.
        return {"ok": True}

    chat_id, text = extracted
    update_id = extract_update_id(body)
    chat_ref = tracing.chat_ref(chat_id)
    logging_setup.bind_request(update_id, chat_ref)

    if not await run_in_threadpool(_rate_limiter.allow, chat_id):
        return Response(status_code=429)

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    if await run_in_threadpool(_seen_before, update_id):
        return {"ok": True}
    if await run_in_threadpool(_is_blocked, chat_id):
        return {"ok": True}  # the owner blocked this chat: no reply, no model call, nothing kept

    text = sanitize_user_text(text)
    if not text:
        return {"ok": True}

    return await _customer_turn(chat_id, text, update_id, chat_ref, extract_sender(body), background_tasks, screen=True, source="text")


async def _customer_turn(
    chat_id: int,
    text: str,
    update_id: int | None,
    chat_ref: str,
    sender: dict | None,
    background_tasks: BackgroundTasks,
    *,
    screen: bool,
    source: str,
):
    """One customer turn: the text they typed, or the sentence a button tap stands for. `screen` is off for a tap, whose
    words were built here from the catalog and not typed by anyone."""
    trace = tracing.TurnTrace(
        update_id=update_id,
        chat_ref=chat_ref,
        domain=config["ACTIVE_DOMAIN"],
        prompt_version=PROMPT_VERSION,
        source=source,
        asked_for_person=bool(tracing.ASKS_FOR_A_PERSON.search(text)),
    )

    # From here to the reply, one message of a chat at a time, so the second message cannot overtake the first.
    async with _chat_turn(chat_id):
        if screen and is_injection_attempt(text):
            # Refuse without an LLM call or Odoo access, and keep the message out of the history where
            # it could steer later turns.
            await _send(chat_id, INJECTION_REFUSAL)
            background_tasks.add_task(_after_blocked, chat_id, text)
            _finish_trace(background_tasks, trace, "blocked", "injection")
            return {"ok": True}

        history = await _history_for(chat_id)
        # run_turn inserts the system prompt at index 0, so rolling back by index
        # would be off by one. Restore a copy of the whole list instead.
        history_before_turn = list(history)
        history.append({"role": "user", "content": text})

        # Any backend can fault here (Odoo, LLM, Telegram, the store). Never return a 500: Telegram would
        # redeliver the same update and could re-run a lead-creating tool call. A redelivery that still
        # arrives is dropped by its update_id.
        tool_events: list = []
        try:
            adapter = await run_in_threadpool(_get_adapter)
            result = await run_in_threadpool(
                run_turn,
                history,
                adapter,
                _get_llm_client(),
                chat_id=chat_id,
                write_limiter=_write_limiter,
                tool_events=tool_events,
                trace=trace,
            )
        except Exception:
            logger.exception("run_turn failed")
            _finish_trace(background_tasks, trace, "error")
            # Drop the half-finished turn (user message, system prompt, tool messages) so a retry doesn't duplicate it.
            history[:] = history_before_turn
            await _send(chat_id, ERROR_REPLY)
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

        # A viewing asked for without a day gets the day buttons; otherwise one button per item the reply describes.
        # The customer taps instead of typing the name, the position or the day.
        viewing = getattr(result, "viewing_item", None)
        markup = await _day_buttons(viewing) if viewing else None
        markup = markup or buttons.results_keyboard(getattr(result, "shown", None) or [], getattr(result, "more", None))
        trace.buttons_shown = len(markup["inline_keyboard"]) if markup else 0
        delivered = await _send(chat_id, result.reply, markup)
        if delivered:
            await _save_chat(chat_id)
        else:
            # A reply the customer never saw is not stored, but this worker still knows what the turn did.
            _unsaved_chats.add(chat_id)

        background_tasks.add_task(_after_reply, chat_id, text, result, delivered, sender)
        _finish_trace(background_tasks, trace, "reply")
        return {"ok": True}


async def _remember_exchange(chat_id: int, said: str, replied: str, background_tasks: BackgroundTasks) -> None:
    """Keep a navigation step (an item the customer picked) in the chat, so the model and a restored chat know what the
    customer is looking at."""
    async with _chat_turn(chat_id):
        history = await _history_for(chat_id)
        history.append({"role": "user", "content": said})
        history.append({"role": "assistant", "content": replied})
        _trim_conversation_history(chat_id)
        await _save_chat(chat_id)
    background_tasks.add_task(_log_turn, chat_id, said, replied, [])
    background_tasks.add_task(_mirror_turn, chat_id, said, replied, [])


async def _send_next_page(chat_id: int, offset: int, key: str, background_tasks: BackgroundTasks) -> None:
    """The next items of the chat's latest search, read from the catalog again and written without the model. The page
    goes into the history as a search and its answer, so "the second one", a hold and the next button all work on it."""
    async with _chat_turn(chat_id):
        history = await _history_for(chat_id)
        try:
            adapter = await run_in_threadpool(_get_adapter)
            found = latest_search(history, adapter.write_tools)
            if found is None or search_key(found[0], found[1]) != key:
                await _send(chat_id, TAP_OUT_OF_DATE)
                return
            name, given, _ = found
            arguments = parse_tool_args(adapter.tool_models()[name], given)
            result = await run_in_threadpool(adapter.execute_tool, name, {**arguments, OFFSET_ARGUMENT: offset})
        except Exception:
            logger.exception("could not read the next page of a search")
            await _send(chat_id, ERROR_REPLY)
            return
        rows = result.get("matches") or []
        if not rows:
            await _send(chat_id, NO_MORE_MATCHES)
            return
        clean = cast(dict, sanitize_tool_result(result))  # a dict goes in, a dict comes out
        text = filter_reply(buttons.page_text(offset, result["count"], clean["matches"]), SYSTEM_PROMPT)
        said = "Show me more."
        call_id = f"page_{uuid.uuid4().hex}"
        history += [
            {"role": "user", "content": said},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(given)}}
            ]},
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(clean)},
            {"role": "assistant", "content": text},
        ]
        _trim_conversation_history(chat_id)
        more = next_page(history, adapter.write_tools, offset + PAGE_SIZE)
        await _send(chat_id, text, buttons.results_keyboard(rows, more))
        await _save_chat(chat_id)
    background_tasks.add_task(_log_turn, chat_id, said, text, [])
    background_tasks.add_task(_mirror_turn, chat_id, said, text, [])


async def _day_buttons(item: dict) -> dict | None:
    """The day buttons for an item, if the catalog still has it as available."""
    try:
        adapter = await run_in_threadpool(_get_adapter)
        row = await run_in_threadpool(adapter.get_item, item["id"])
    except Exception:
        logger.exception("could not look up the item for the day buttons")
        return None
    if row is None or row.get("status") != "available":
        return None
    return buttons.day_keyboard(item["id"], dt.date.today())


async def _handle_tap(tap: dict, update_id: int | None, background_tasks: BackgroundTasks):
    """A tap on a button under a list of results. A tap that only moves along (pick an item, pick a day) is answered here
    from the catalog. A tap that asks for something (hold, viewing, call) becomes the sentence the customer would have
    typed and runs as a normal turn, so the name and contact are still asked for and every guard still applies."""
    chat_id = tap["chat_id"]
    chat_ref = tracing.chat_ref(chat_id)
    logging_setup.bind_request(update_id, chat_ref)
    await run_in_threadpool(answer_callback_query, tap["id"])  # first, so the button stops spinning whatever follows

    if not await run_in_threadpool(_rate_limiter.allow, chat_id):
        return Response(status_code=429)
    if await run_in_threadpool(_seen_before, update_id):
        return {"ok": True}
    if await run_in_threadpool(_is_blocked, chat_id):
        return {"ok": True}

    parsed = buttons.parse_callback(tap["data"])
    if parsed is None:
        await _send(chat_id, TAP_OUT_OF_DATE)
        return {"ok": True}
    kind, item_id, rest = parsed
    if kind == buttons.MORE:
        await _send_next_page(chat_id, item_id, rest[0], background_tasks)  # item_id holds the offset
        return {"ok": True}

    today = dt.date.today()
    if kind in (buttons.DAY, buttons.SLOT):
        day = dt.date.fromisoformat(rest[0])
        if day < today or day > today + dt.timedelta(days=VIEWING_WINDOW_DAYS):
            await _send(chat_id, TAP_OUT_OF_DATE)
            return {"ok": True}

    try:
        adapter = await run_in_threadpool(_get_adapter)
        row = await run_in_threadpool(adapter.get_item, item_id)
    except Exception:
        logger.exception("could not look up the item for a button tap")
        await _send(chat_id, ERROR_REPLY)
        return {"ok": True}
    if row is None or row.get("status") != "available":
        await _send(chat_id, TAP_ITEM_GONE)
        return {"ok": True}

    if kind == buttons.PICK:
        card = buttons.pick_text(row)
        await _send(chat_id, card, buttons.actions_keyboard(item_id))
        await _remember_exchange(chat_id, f"I'm looking at the {row['name']}.", card, background_tasks)
        return {"ok": True}
    if kind == buttons.ACT and rest == ["view"]:
        await _send(chat_id, buttons.day_text(row), buttons.day_keyboard(item_id, today))
        return {"ok": True}
    if kind == buttons.DAY:
        await _send(chat_id, buttons.slot_text(rest[0]), buttons.slot_keyboard(item_id, rest[0]))
        return {"ok": True}

    words = buttons.customer_words(kind, row, rest)
    if words is None:
        await _send(chat_id, TAP_OUT_OF_DATE)
        return {"ok": True}
    if tap.get("message_id") is not None:
        await run_in_threadpool(clear_keyboard, chat_id, tap["message_id"])  # one tap, one request
    return await _customer_turn(
        chat_id, sanitize_user_text(words), update_id, chat_ref, tap.get("sender"), background_tasks, screen=False, source="button"
    )


def _handle_alert_update(body) -> None:
    # Built here, on the worker thread: the owner's dependencies include the adapter, whose first
    # build logs in to Odoo.
    owner_bot.handle_update(body, _owner_deps())


@app.post("/webhook/alerts")
async def alerts_webhook(request: Request):
    """Updates from the alert bot: the owner pressing a button or replying to an alert.

    Fails closed: with no secret configured the route refuses everything, and the secret
    is compared in constant time. Beyond that, owner_bot ignores any update that does not
    come from the owner's own chat."""
    expected = os.environ.get("TELEGRAM_ALERTS_WEBHOOK_SECRET")
    if not expected or webhook_secret_problem("TELEGRAM_ALERTS_WEBHOOK_SECRET", expected):
        return Response(status_code=503)
    if not _secret_matches(request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "", expected):
        return Response(status_code=401)

    body = await _json_object(request)
    if body is None:
        return Response(status_code=400)
    update_id = extract_update_id(body)
    logging_setup.bind_request(update_id, None)
    if await run_in_threadpool(_seen_before, update_id, "owner", _seen_owner_updates):
        return {"ok": True}
    try:
        await run_in_threadpool(_handle_alert_update, body)
    except Exception:
        logger.exception("could not handle an alert-bot update")
    return {"ok": True}


@app.post("/webhook/odoo-status")
async def odoo_status_webhook(request: Request, background_tasks: BackgroundTasks):
    """Odoo reporting that a catalog item's status changed. The dashboard's row is updated after the
    response, so Odoo (which waits for this inside its own write) is never held up by Supabase.

    Fails closed: with no usable ODOO_WEBHOOK_SECRET the route answers 503 and does nothing. The secret
    is checked in constant time before the body is read, and the body is capped at MAX_BODY_BYTES."""
    expected = os.environ.get("ODOO_WEBHOOK_SECRET")
    if odoo_status.secret_problem(expected):
        return Response(status_code=503)
    assert expected is not None
    if not _secret_matches(request.headers.get(odoo_status.SECRET_HEADER) or "", expected):
        return Response(status_code=401)
    try:
        raw = await odoo_status.read_body(request)
    except odoo_status.BodyTooLarge:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    try:
        event = odoo_status.parse_event(json.loads(raw))
    except (ValueError, RecursionError) as exc:  # a body that is not JSON, or JSON that is not a status event
        reason = str(exc) if isinstance(exc, odoo_status.InvalidEvent) else "the body is not JSON"
        return JSONResponse({"detail": reason}, status_code=422)
    background_tasks.add_task(odoo_status.process, event)
    return JSONResponse({"status": "accepted"}, status_code=202)
