"""GET /ready tells a load balancer or a person whether the engine can do its job, without calling the
model and without leaking a secret. /health stays the cheap liveness probe."""
import asyncio
import threading
import time

import httpx
import pytest

import engine.main as main_module
from engine import store as store_module
from engine.main import app, config
from engine.odoo_client import OdooLoginError

PASSWORD = config["ODOO_PASSWORD"]
KEY = config.get("OPENROUTER_API_KEY", "")


class FakeOdoo:
    def __init__(self, error=None):
        self.error = error
        self.uid = 7
        self.calls = 0

    def search_read(self, model, domain, fields, **options):
        self.calls += 1
        if self.error:
            raise self.error
        return [{"id": self.uid}]


class FakeAdapter:
    def __init__(self, odoo):
        self.odoo = odoo


@pytest.fixture
def stack(monkeypatch):
    """A healthy engine: Odoo answers, the store works, a key and a model are configured."""
    odoo = FakeOdoo()
    monkeypatch.setattr(main_module, "_get_adapter", lambda: FakeAdapter(odoo))
    monkeypatch.setattr(main_module, "READY_CHECK_TIMEOUT_SECONDS", 1.0)
    return odoo


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine.test")


def get_ready():
    async def go():
        async with _client() as ac:
            return await ac.get("/ready")

    return asyncio.run(go())


def test_everything_ok_is_a_200_with_each_check_listed(stack):
    response = get_ready()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"odoo", "store", "llm"}
    assert all(check["ok"] for check in body["checks"].values())
    assert stack.calls == 1


def test_odoo_down_is_a_503_that_names_the_check(monkeypatch, stack):
    stack.error = ConnectionRefusedError(f"connect to http://admin:{PASSWORD}@odoo:8069 refused")
    response = get_ready()
    body = response.json()
    assert response.status_code == 503 and body["status"] == "not_ready"
    assert body["checks"]["odoo"]["ok"] is False and "unreachable" in body["checks"]["odoo"]["reason"]
    assert body["checks"]["store"]["ok"] and body["checks"]["llm"]["ok"]
    assert PASSWORD not in response.text and "odoo:8069" not in response.text


def test_a_rejected_login_is_reported_as_such(monkeypatch):
    def refuse():
        raise OdooLoginError(f"Odoo rejected the login for user admin on db leadgate {PASSWORD}")

    monkeypatch.setattr(main_module, "_get_adapter", refuse)
    response = get_ready()
    assert response.status_code == 503
    assert "login" in response.json()["checks"]["odoo"]["reason"]
    assert PASSWORD not in response.text


def test_the_cached_adapter_is_reused_not_logged_in_again(monkeypatch):
    builds = []
    odoo = FakeOdoo()
    monkeypatch.setattr(main_module, "_adapter", None)
    monkeypatch.setattr(main_module, "OdooClient", lambda *a, **k: builds.append(a) or odoo)
    monkeypatch.setitem(main_module.ADAPTERS, config["ACTIVE_DOMAIN"], FakeAdapter)
    monkeypatch.setattr(main_module, "READY_CHECK_TIMEOUT_SECONDS", 1.0)
    assert get_ready().status_code == 200
    assert get_ready().status_code == 200
    assert len(builds) == 1  # the second probe used the client the first one built


def test_a_failed_check_can_recover(stack):
    stack.error = ConnectionRefusedError("down")
    assert get_ready().status_code == 503
    stack.error = None
    assert get_ready().status_code == 200  # nothing is remembered between probes


def test_a_slow_check_times_out_without_holding_the_loop(monkeypatch, stack):
    release = threading.Event()

    def slow_search(*args, **kwargs):
        release.wait(timeout=3)
        return [{"id": 7}]

    stack.search_read = slow_search  # type: ignore[method-assign]
    monkeypatch.setattr(main_module, "READY_CHECK_TIMEOUT_SECONDS", 0.3)

    async def go():
        async with _client() as ac:
            started = time.monotonic()
            probe = asyncio.create_task(ac.get("/ready"))
            await asyncio.sleep(0.05)
            health = await ac.get("/health")  # answered while the probe is still waiting on Odoo
            health_at = time.monotonic() - started
            response = await probe
            return health, health_at, response, time.monotonic() - started

    try:
        health, health_at, response, total = asyncio.run(go())
    finally:
        release.set()
    assert health.status_code == 200 and health_at < 0.25
    assert response.status_code == 503
    assert "timed out" in response.json()["checks"]["odoo"]["reason"]
    assert total < 2.0


def test_a_store_that_does_not_answer_fails_the_store_check(monkeypatch, stack):
    class BrokenStore:
        def ping(self):
            raise RuntimeError("https://user:hunter2@store.test timed out")

    monkeypatch.setattr(store_module, "_store", BrokenStore())
    response = get_ready()
    assert response.status_code == 503
    assert response.json()["checks"]["store"]["ok"] is False
    assert "hunter2" not in response.text and "store.test" not in response.text


def test_the_in_memory_store_is_always_ok():
    assert store_module.InMemoryStore().ping() is None


def test_no_llm_key_fails_the_llm_check_without_calling_the_model(monkeypatch, stack):
    monkeypatch.setattr(main_module, "config", {k: v for k, v in config.items() if not k.endswith("_API_KEY")})
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: pytest.fail("/ready must not build or call the model"))
    response = get_ready()
    assert response.status_code == 503
    assert response.json()["checks"]["llm"] == {"ok": False, "reason": "no LLM API key configured"}


def test_no_model_configured_fails_the_llm_check(monkeypatch, stack):
    monkeypatch.setattr(main_module, "configured_llm_models", lambda cfg: [])
    body = get_ready().json()
    assert body["checks"]["llm"] == {"ok": False, "reason": "no model configured"}


def test_no_secret_appears_in_any_response(stack):
    secrets = [PASSWORD, KEY, config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_WEBHOOK_SECRET"], config["ODOO_URL"]]
    ok = get_ready().text
    stack.error = ConnectionRefusedError(" ".join(secrets))
    bad = get_ready().text
    for text in (ok, bad):
        for secret in secrets:
            assert secret and secret not in text


def test_health_is_still_a_cheap_liveness_probe(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: pytest.fail("/health must not touch Odoo"))

    async def go():
        async with _client() as ac:
            return await ac.get("/health")

    response = asyncio.run(go())
    assert response.status_code == 200 and response.json() == {"status": "ok"}


# A failed Odoo login is remembered briefly, so callers fail fast instead of each retrying it


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def factory(monkeypatch):
    """A fake Odoo client factory (counts logins, can be made to fail) and a fake clock."""

    class Factory:
        def __init__(self):
            self.calls = 0
            self.error = ConnectionRefusedError("odoo is down")
            self.odoo = FakeOdoo()

        def __call__(self, *args, **kwargs):
            self.calls += 1
            if self.error:
                raise self.error
            return self.odoo

    f = Factory()
    clock = Clock()
    monkeypatch.setattr(main_module, "_adapter", None)
    monkeypatch.setattr(main_module, "_adapter_failure", None)
    monkeypatch.setattr(main_module, "_adapter_clock", clock)
    monkeypatch.setattr(main_module, "OdooClient", f)
    monkeypatch.setitem(main_module.ADAPTERS, config["ACTIVE_DOMAIN"], FakeAdapter)
    monkeypatch.setattr(main_module, "READY_CHECK_TIMEOUT_SECONDS", 1.0)
    f.clock = clock  # type: ignore[attr-defined]
    return f


def test_a_failed_login_is_not_retried_straight_away(factory):
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    assert factory.calls == 1


def test_the_login_is_tried_again_after_the_cooldown(factory):
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    factory.clock.now += main_module.ADAPTER_FAILURE_COOLDOWN_SECONDS - 0.1
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    assert factory.calls == 1
    factory.clock.now += 0.2
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    assert factory.calls == 2


def test_a_success_clears_the_cooldown_and_is_cached(factory):
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    factory.clock.now += main_module.ADAPTER_FAILURE_COOLDOWN_SECONDS + 1
    factory.error = None
    adapter = main_module._get_adapter()
    assert main_module._adapter_failure is None
    assert main_module._get_adapter() is adapter and factory.calls == 2


def test_the_cooldown_lasts_ten_seconds():
    assert main_module.ADAPTER_FAILURE_COOLDOWN_SECONDS == 10


def test_ready_answers_503_at_once_while_a_failed_login_is_cooling_down(factory):
    first = get_ready()
    assert first.status_code == 503 and first.json()["checks"]["odoo"]["reason"] == "unreachable"
    started = time.monotonic()
    second = get_ready()
    assert second.status_code == 503 and time.monotonic() - started < 0.5
    assert factory.calls == 1  # the second probe did not log in again
    factory.error = None
    factory.clock.now += main_module.ADAPTER_FAILURE_COOLDOWN_SECONDS + 1
    assert get_ready().status_code == 200


def test_customer_turns_fail_fast_too_while_the_login_cools_down(factory):
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    started = time.monotonic()
    with pytest.raises(ConnectionRefusedError):
        main_module._get_adapter()
    assert time.monotonic() - started < 0.5 and factory.calls == 1
