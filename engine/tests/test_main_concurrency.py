"""The webhook must not run blocking work on the event loop, and one chat's messages stay in order."""
import asyncio
import threading
import time

import httpx
import respx

import engine.main as main_module
from engine.core.agent_loop import AgentTurnResult
from engine.main import app, config, conversation_history
from engine.rate_limiter import FixedWindowRateLimiter

HEADERS = {"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]}
SEND_URL = f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"


def _update(update_id: int, chat_id: int, text: str = "Hello there") -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id, "type": "private"}, "text": text},
    }


def _prepare(monkeypatch, run_turn):
    conversation_history.clear()
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "run_turn", run_turn)
    monkeypatch.setattr(
        main_module,
        "_rate_limiter",
        FixedWindowRateLimiter(main_module.RATE_LIMIT_MAX_REQUESTS, main_module.RATE_LIMIT_WINDOW_SECONDS),
    )
    monkeypatch.setattr(main_module, "_seen_updates", main_module.OrderedDict())


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine.test")


async def _wait_for(event: threading.Event, seconds: float = 1.0) -> bool:
    return await asyncio.get_running_loop().run_in_executor(None, event.wait, seconds)


@respx.mock
def test_a_slow_turn_does_not_freeze_health_or_other_chats(monkeypatch):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    entered = threading.Event()
    release = threading.Event()
    state = {"slow_timed_out": False}

    def run_turn(history, adapter, llm, **kwargs):
        if kwargs["chat_id"] == 1:
            entered.set()
            # Bounded, so a regression fails the assertion below instead of hanging the suite.
            state["slow_timed_out"] = not release.wait(timeout=2)
        return AgentTurnResult(reply="ok")

    _prepare(monkeypatch, run_turn)

    async def scenario():
        async with _client() as ac:
            slow = asyncio.create_task(ac.post("/webhook/telegram", json=_update(1, 1), headers=HEADERS))
            assert await _wait_for(entered)
            health = await ac.get("/health")
            other = await ac.post("/webhook/telegram", json=_update(2, 2), headers=HEADERS)
            still_blocked = not release.is_set()
            release.set()
            slow_response = await slow
        return health, other, slow_response, still_blocked

    health, other, slow_response, still_blocked = asyncio.run(scenario())

    assert still_blocked
    assert not state["slow_timed_out"], "the slow turn held the event loop until it timed out"
    assert health.status_code == 200
    assert other.status_code == 200
    assert slow_response.status_code == 200


@respx.mock
def test_two_messages_from_one_chat_run_one_after_another(monkeypatch):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    guard = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    def run_turn(history, adapter, llm, **kwargs):
        with guard:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.2)
        with guard:
            state["active"] -= 1
        return AgentTurnResult(reply="ok")

    _prepare(monkeypatch, run_turn)

    async def scenario():
        async with _client() as ac:
            return await asyncio.gather(
                ac.post("/webhook/telegram", json=_update(10, 7, "first"), headers=HEADERS),
                ac.post("/webhook/telegram", json=_update(11, 7, "second"), headers=HEADERS),
            )

    responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [200, 200]
    assert state["calls"] == 2
    assert state["max_active"] == 1
    assert sorted(m["content"] for m in conversation_history[7] if m["role"] == "user") == ["first", "second"]
    assert main_module._chat_locks == {}  # nothing left behind, though one request had to queue


@respx.mock
def test_different_chats_do_run_at_the_same_time(monkeypatch):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    both_inside = threading.Barrier(2, timeout=2)
    met = []

    def run_turn(history, adapter, llm, **kwargs):
        both_inside.wait()
        met.append(kwargs["chat_id"])
        return AgentTurnResult(reply="ok")

    _prepare(monkeypatch, run_turn)

    async def scenario():
        async with _client() as ac:
            return await asyncio.gather(
                ac.post("/webhook/telegram", json=_update(20, 1), headers=HEADERS),
                ac.post("/webhook/telegram", json=_update(21, 2), headers=HEADERS),
            )

    responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(met) == [1, 2]


@respx.mock
def test_chat_lock_registry_does_not_grow_with_chats(monkeypatch):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    _prepare(monkeypatch, lambda history, adapter, llm, **kwargs: AgentTurnResult(reply="ok"))

    async def scenario():
        async with _client() as ac:
            for n in range(25):
                response = await ac.post("/webhook/telegram", json=_update(100 + n, 5000 + n), headers=HEADERS)
                assert response.status_code == 200

    asyncio.run(scenario())

    assert len(main_module._chat_locks) == 0


@respx.mock
def test_a_slow_odoo_login_does_not_freeze_health(monkeypatch):
    """The adapter is built lazily on the first message and that logs in to Odoo, a blocking call."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    entered = threading.Event()
    release = threading.Event()
    state = {"login_timed_out": False}

    def slow_adapter():
        entered.set()
        state["login_timed_out"] = not release.wait(timeout=2)
        return object()

    _prepare(monkeypatch, lambda history, adapter, llm, **kwargs: AgentTurnResult(reply="ok"))
    monkeypatch.setattr(main_module, "_get_adapter", slow_adapter)

    async def scenario():
        async with _client() as ac:
            slow = asyncio.create_task(ac.post("/webhook/telegram", json=_update(1, 1), headers=HEADERS))
            assert await _wait_for(entered)
            health = await ac.get("/health")
            still_blocked = not release.is_set()
            release.set()
            return health, await slow, still_blocked

    health, slow_response, still_blocked = asyncio.run(scenario())

    assert still_blocked
    assert not state["login_timed_out"], "the Odoo login held the event loop"
    assert health.status_code == 200
    assert slow_response.status_code == 200


def test_two_threads_asking_for_the_adapter_build_one(monkeypatch):
    built = []

    class SlowOdoo:
        def __init__(self, *args):
            time.sleep(0.2)
            built.append(self)

    monkeypatch.setattr(main_module, "_adapter", None)
    monkeypatch.setattr(main_module, "OdooClient", SlowOdoo)
    results = []
    threads = [threading.Thread(target=lambda: results.append(main_module._get_adapter())) for _ in range(4)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1
    assert len({id(a) for a in results}) == 1


def test_the_rate_limiter_survives_many_threads_and_keeps_its_cap():
    from engine.rate_limiter import FixedWindowRateLimiter

    limiter = FixedWindowRateLimiter(10, 60)
    errors = []
    start = threading.Barrier(8)

    def churn(worker):
        start.wait()
        try:
            for n in range(3000):
                limiter.allow(worker * 100000 + n)  # distinct keys: grows the dict, triggers sweeps
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    shared = FixedWindowRateLimiter(10, 60)
    allowed = []
    go = threading.Barrier(40)

    def hit():
        go.wait()
        allowed.append(shared.allow(7))

    threads = [threading.Thread(target=hit) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert allowed.count(True) == 10


@respx.mock
def test_same_chat_messages_keep_their_order_even_if_the_first_lookup_is_slow(monkeypatch):
    """The history lookup runs on a thread before the turn. If the lock is only taken after it,
    whichever lookup finishes first goes first."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    first_lookup_started = threading.Event()
    let_first_lookup_finish = threading.Event()
    lookups = []
    ran = []

    def slow_first_lookup(*args, **kwargs):
        lookups.append(args)
        if len(lookups) == 1:
            first_lookup_started.set()
            let_first_lookup_finish.wait(timeout=2)
        return []

    def run_turn(history, adapter, llm, **kwargs):
        ran.append(history[-1]["content"])
        return AgentTurnResult(reply="ok")

    _prepare(monkeypatch, run_turn)
    monkeypatch.setattr(main_module.store_module.get_store(), "load_history", slow_first_lookup)

    async def scenario():
        async with _client() as ac:
            first = asyncio.create_task(ac.post("/webhook/telegram", json=_update(30, 9, "first"), headers=HEADERS))
            assert await _wait_for(first_lookup_started)
            second = asyncio.create_task(ac.post("/webhook/telegram", json=_update(31, 9, "second"), headers=HEADERS))
            await asyncio.sleep(0.1)  # long enough for the second request to run ahead if nothing stops it
            let_first_lookup_finish.set()
            return await first, await second

    first, second = asyncio.run(scenario())

    assert (first.status_code, second.status_code) == (200, 200)
    assert ran == ["first", "second"]


@respx.mock
def test_health_answers_even_when_every_worker_thread_is_busy(monkeypatch):
    import anyio.to_thread

    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    entered = threading.Event()
    release = threading.Event()

    def run_turn(history, adapter, llm, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return AgentTurnResult(reply="ok")

    _prepare(monkeypatch, run_turn)

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1  # one worker thread, and the slow turn holds it
        try:
            async with _client() as ac:
                slow = asyncio.create_task(ac.post("/webhook/telegram", json=_update(40, 3), headers=HEADERS))
                assert await _wait_for(entered)
                try:
                    health = await asyncio.wait_for(ac.get("/health"), timeout=1)
                except asyncio.TimeoutError:
                    health = None
                release.set()
                await slow
                return health
        finally:
            limiter.total_tokens = original

    health = asyncio.run(scenario())

    assert health is not None, "/health queued behind the busy worker threads"
    assert health.status_code == 200


def test_a_slow_odoo_login_does_not_freeze_health_on_the_alerts_webhook(monkeypatch):
    """The owner's dependencies include the adapter, whose first build logs in to Odoo."""
    from types import SimpleNamespace

    entered = threading.Event()
    release = threading.Event()
    state = {"login_timed_out": False}

    def slow_adapter():
        entered.set()
        state["login_timed_out"] = not release.wait(timeout=2)
        return SimpleNamespace(odoo=object())

    handled = []
    monkeypatch.setattr(main_module, "_get_adapter", slow_adapter)
    monkeypatch.setattr(main_module.store_module, "get_store", lambda: object())
    monkeypatch.setattr(main_module.owner_bot, "handle_update", lambda body, deps: handled.append(body))
    monkeypatch.setattr(main_module, "_seen_owner_updates", main_module.OrderedDict())
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "alerts-secret-0123456789")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "4242")
    alert_headers = {"X-Telegram-Bot-Api-Secret-Token": "alerts-secret-0123456789"}

    async def scenario():
        async with _client() as ac:
            slow = asyncio.create_task(ac.post("/webhook/alerts", json={"update_id": 77}, headers=alert_headers))
            assert await _wait_for(entered)
            health = await ac.get("/health")
            still_blocked = not release.is_set()
            release.set()
            return health, await slow, still_blocked

    health, slow_response, still_blocked = asyncio.run(scenario())

    assert still_blocked
    assert not state["login_timed_out"], "the owner webhook held the event loop during the Odoo login"
    assert health.status_code == 200
    assert slow_response.status_code == 200
    assert handled == [{"update_id": 77}]
