"""SupabaseStore's own behaviour: what it sends, what it refuses to leak, and how it fails."""
import json
import logging

import httpx
import pytest

from engine import store as store_module
from engine import supabase_rest
from engine.store import InMemoryStore, StoreUnavailable, SupabaseStore
from engine.tests.fake_postgrest import FakePostgrest

BASE = "https://proj.supabase.co"
KEY = "service-key-not-real"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def world():
    fake, clock = FakePostgrest(), Clock()
    return fake, clock, SupabaseStore(BASE, KEY, client=fake.client(), clock=clock)


# What goes over the wire

def test_every_request_carries_the_service_key(world):
    fake, _, store = world
    store.log_turn(1, "cars", "q", "a", [])
    store.ping()
    assert all(r.headers["apikey"] == KEY and r.headers["authorization"] == f"Bearer {KEY}" for r in fake.requests)


def test_the_claim_is_one_insert_that_ignores_duplicates_not_a_read_then_a_write(world):
    fake, _, store = world
    store.claim_update("customer", 5)
    [request] = fake.requests
    assert request.method == "POST" and request.url.path.endswith("/processed_updates")
    assert request.url.params["on_conflict"] == "source,update_id"
    assert "ignore-duplicates" in request.headers["prefer"] and "return=representation" in request.headers["prefer"]
    assert json.loads(request.content) == {"source": "customer", "update_id": 5}


def test_the_rate_limit_is_one_call_to_the_database_function(world):
    fake, _, store = world
    store.hit_rate_limit("webhook:7", 60, 10)
    [request] = fake.requests
    assert request.url.path.endswith("/rpc/rate_limit_hit")
    assert json.loads(request.content) == {"p_key": "webhook:7", "p_window_seconds": 60, "p_max": 10}


def test_a_stored_username_is_not_overwritten_by_an_absent_one(world):
    fake, _, store = world
    store.save_lead_chat(71, chat_id=5, kind="lead", item_name="x", customer_name="y")
    assert "username" not in json.loads(fake.requests[0].content)
    store.save_lead_chat(71, chat_id=5, kind="lead", item_name="x", customer_name="y", username="sarah_c")
    assert json.loads(fake.requests[1].content)["username"] == "sarah_c"


def test_history_asks_for_unblocked_turns_newest_first_with_a_limit(world):
    fake, _, store = world
    store.load_history(42, 7)
    params = fake.requests[0].url.params
    assert params["chat_id"] == "eq.42" and params["blocked"] == "eq.false" and params["order"] == "id.desc" and params["limit"] == "7"


def test_purge_passes_the_retention_periods(world):
    fake, _, store = world
    store.purge_expired()
    assert json.loads(fake.requests[0].content) == {"p_update_days": store_module.UPDATE_RETENTION_DAYS, "p_lead_chat_days": store_module.LEAD_CHAT_RETENTION_DAYS}


# Failure

def test_a_network_failure_raises_store_unavailable_naming_no_address_or_key(world):
    fake, _, store = world
    fake.fail_with = httpx.ConnectError(f"cannot reach {BASE} with {KEY}")
    with pytest.raises(StoreUnavailable) as caught:
        store.load_history(1)
    assert BASE not in str(caught.value) and KEY not in str(caught.value) and "proj" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("status", [401, 404, 409])
def test_a_client_error_raises_but_does_not_stop_the_store_being_tried_again(world, status):
    fake, _, store = world
    fake.fail_with = status
    with pytest.raises(StoreUnavailable, match=str(status)):
        store.get_lead_chat(1)
    fake.fail_with = None
    assert store.get_lead_chat(1) is None


def test_after_a_failure_the_store_is_not_tried_again_until_the_cooldown_has_passed(world):
    fake, clock, store = world
    fake.fail_with = httpx.ReadTimeout("slow")
    with pytest.raises(StoreUnavailable):
        store.load_history(1)
    fake.fail_with = None
    calls = len(fake.requests)
    with pytest.raises(StoreUnavailable, match="a moment ago"):
        store.load_history(1)
    assert len(fake.requests) == calls  # no request went out
    clock.now += store_module.UNAVAILABLE_COOLDOWN_SECONDS + 1
    assert store.load_history(1) == []


def test_a_server_error_also_starts_the_cooldown(world):
    fake, _, store = world
    fake.fail_with = 503
    with pytest.raises(StoreUnavailable):
        store.get_lead_chat(1)
    fake.fail_with = None
    with pytest.raises(StoreUnavailable, match="a moment ago"):
        store.get_lead_chat(1)


def test_the_readiness_ping_always_tries_even_during_the_cooldown(world):
    fake, _, store = world
    fake.fail_with = httpx.ConnectError("down")
    with pytest.raises(StoreUnavailable):
        store.ping()
    fake.fail_with = None
    store.ping()  # recovers at once, so /ready shows a recovered store without waiting


def test_a_success_ends_the_cooldown(world):
    fake, clock, store = world
    fake.fail_with = 500
    with pytest.raises(StoreUnavailable):
        store.get_lead_chat(1)
    fake.fail_with = None
    store.ping()
    assert store.get_lead_chat(1) is None


# Configuration

def test_from_env_is_none_without_a_url_or_a_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", KEY)
    assert SupabaseStore.from_env() is None
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    assert SupabaseStore.from_env() is None


def test_from_env_builds_a_store_when_both_are_set(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE + "/rest/v1/")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", KEY)
    built = SupabaseStore.from_env()
    assert built is not None and built._rest == f"{BASE}/rest/v1"


def test_a_url_that_is_not_an_address_is_refused_by_name_without_echoing_it(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "not-a-url-with-a-secret-token")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", KEY)
    with pytest.raises(ValueError, match="SUPABASE_URL") as caught:
        SupabaseStore.from_env()
    assert "secret-token" not in str(caught.value)


def test_get_store_is_memory_without_supabase_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(store_module, "_store", None)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with caplog.at_level(logging.WARNING):
        built = store_module.get_store()
    assert isinstance(built, InMemoryStore) and store_module.get_store() is built
    assert "reset on restart" in caplog.text


def test_get_store_is_supabase_when_configured(monkeypatch):
    monkeypatch.setattr(store_module, "_store", None)
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", KEY)
    assert isinstance(store_module.get_store(), SupabaseStore)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://proj.supabase.co", "https://proj.supabase.co"),
        ("https://proj.supabase.co/", "https://proj.supabase.co"),
        ("https://proj.supabase.co/rest/v1", "https://proj.supabase.co"),
        ("  https://proj.supabase.co/rest/v1/  ", "https://proj.supabase.co"),
        ("http://localhost:54321", "http://localhost:54321"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_the_project_address_is_normalised(given, expected, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert supabase_rest.base_url(given) == expected


@pytest.mark.parametrize("bad", ["proj.supabase.co", "ftp://proj.supabase.co", "https://", "https://proj.supabase.co?x=1"])
def test_an_address_that_is_not_http_is_refused(bad):
    with pytest.raises(ValueError, match="SUPABASE_URL"):
        supabase_rest.base_url(bad)


def test_the_project_ref_is_the_first_label(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefgh.supabase.co/rest/v1/")
    assert supabase_rest.project_ref() == "abcdefgh"
    monkeypatch.delenv("SUPABASE_URL")
    assert supabase_rest.project_ref() is None
