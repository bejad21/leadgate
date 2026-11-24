"""POST /webhook/odoo-status: who may call it, what it accepts, and what it does to catalog_items."""
import asyncio
import copy
import json
import logging
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import odoo_status
from engine.main import app
from engine.tests.fake_postgrest import FakePostgrest

SECRET = "odoo-status-secret-0123456789"
HEADER = odoo_status.SECRET_HEADER
client = TestClient(app)


@pytest.fixture
def supabase(monkeypatch):
    fake = FakePostgrest()
    monkeypatch.setenv("ODOO_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key-not-real")
    monkeypatch.setattr(odoo_status, "_new_client", fake.client)
    monkeypatch.setattr(odoo_status.time, "sleep", lambda _: None)
    return fake


def event(**overrides):
    body = {"id": 7, "domain_type": "cars", "status": "sold", "name": "2020 Toyota Camry", "price": 21834.0, "write_date": "2026-09-20 10:00:00"}
    body.update(overrides)
    return body


def send(body, secret: str | None = SECRET, raw=None):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[HEADER] = secret
    return client.post("/webhook/odoo-status", content=raw if raw is not None else json.dumps(body), headers=headers)


def calls_to(fake, name="apply_catalog_event"):
    return [r for r in fake.requests if r.url.path.endswith(f"/rpc/{name}")]


# Who may call it

def test_without_a_secret_configured_the_route_is_closed(supabase, monkeypatch):
    monkeypatch.delenv("ODOO_WEBHOOK_SECRET")
    assert send(event(), secret=SECRET).status_code == 503
    assert supabase.requests == []


@pytest.mark.parametrize("short", ["short", "a" * (odoo_status.MIN_SECRET_LENGTH - 1)])
def test_a_secret_too_short_to_protect_anything_leaves_the_route_closed(supabase, monkeypatch, short):
    monkeypatch.setenv("ODOO_WEBHOOK_SECRET", short)
    assert send(event(), secret=short).status_code == 503
    assert supabase.requests == []


def test_a_missing_secret_header_is_refused_and_nothing_is_written(supabase):
    assert send(event(), secret=None).status_code == 401
    assert supabase.requests == []


@pytest.mark.parametrize("wrong", ["", "wrong", SECRET + "x", SECRET[:-1], SECRET.upper()])
def test_a_wrong_secret_is_refused_and_nothing_is_written(supabase, wrong):
    assert send(event(), secret=wrong).status_code == 401
    assert supabase.requests == []


def test_a_non_ascii_secret_is_a_401_not_a_crash(supabase):
    response = client.post("/webhook/odoo-status", content=json.dumps(event()), headers={HEADER.encode(): "sécret".encode("utf-8")})
    assert response.status_code == 401


def test_the_secret_is_checked_before_the_body_is_read(supabase):
    assert send(None, secret="wrong", raw="{not json").status_code == 401


# What it accepts

def test_a_valid_event_is_accepted_and_the_row_is_written(supabase):
    response = send(event())
    assert response.status_code == 202 and response.json() == {"status": "accepted"}
    assert supabase.catalog == {7: {"domain_type": "cars", "name": "2020 Toyota Camry", "price": 21834.0, "status": "sold", "updated_at": "2026-09-20T10:00:00+00:00"}}


def test_the_database_function_gets_the_write_time_as_utc(supabase):
    send(event(write_date="2026-09-20T12:00:00+02:00"))
    assert json.loads(calls_to(supabase)[0].content)["p_written_at"] == "2026-09-20T10:00:00+00:00"


def test_the_call_to_supabase_uses_the_service_key(supabase):
    send(event())
    request = calls_to(supabase)[0]
    assert request.headers["apikey"] == "service-key-not-real" and request.url.host == "proj.supabase.co"


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 0}, {"id": -1}, {"id": "7"}, {"id": True}, {"id": 1.5}, {"id": None}, {"id": 2**60},
        {"domain_type": ""}, {"domain_type": "x" * 41}, {"domain_type": "cars; drop"}, {"domain_type": 5},
        {"status": "weird"}, {"status": None}, {"status": ["sold"]},
        {"name": 5}, {"name": "x" * 301}, {"name": None},
        {"price": -1}, {"price": "10"}, {"price": True}, {"price": float("inf")}, {"price": 1e12}, {"price": None},
        {"write_date": ""}, {"write_date": "yesterday"}, {"write_date": 5}, {"write_date": None}, {"write_date": "2999-01-01 00:00:00"},
    ],
)
def test_a_malformed_event_is_a_422_and_writes_nothing(supabase, bad):
    response = send(event(**bad))
    assert response.status_code == 422 and supabase.requests == []


@pytest.mark.parametrize("raw", ["{not json", "", "[]", "null", "5", '"x"', "[" * 5000])
def test_a_body_that_is_not_an_object_is_a_422(supabase, raw):
    assert send(None, raw=raw).status_code == 422
    assert supabase.requests == []


def test_a_missing_field_is_a_422(supabase):
    body = event()
    del body["write_date"]
    assert send(body).status_code == 422


def test_the_422_says_what_is_wrong_but_not_what_was_sent(supabase):
    response = send(event(status="SECRET-VALUE-9"))
    assert "status must be one of" in response.json()["detail"] and "SECRET-VALUE-9" not in response.text


def test_the_body_is_capped(supabase):
    padding = "x" * (odoo_status.MAX_BODY_BYTES + 10)
    assert send(event(name=padding)).status_code == 413
    assert supabase.requests == []


def test_the_cap_holds_even_when_the_declared_length_lies(supabase):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine.test") as ac:
            async def chunks():
                for _ in range(3):
                    yield b"x" * odoo_status.MAX_BODY_BYTES
            return await ac.post("/webhook/odoo-status", content=chunks(), headers={HEADER: SECRET})

    assert asyncio.run(scenario()).status_code == 413


def test_a_body_exactly_at_the_cap_is_still_read(supabase):
    room = odoo_status.MAX_BODY_BYTES - len(json.dumps(event(pad="")))
    body = json.dumps(event(pad="p" * room))
    assert len(body) == odoo_status.MAX_BODY_BYTES
    assert send(None, raw=body).status_code == 202


def test_the_route_does_not_accept_get(supabase):
    assert client.get("/webhook/odoo-status", headers={HEADER: SECRET}).status_code == 405


# What it does to catalog_items

def test_a_replayed_event_changes_nothing(supabase):
    send(event())
    first = copy.deepcopy(supabase.catalog)
    send(event())
    assert supabase.catalog == first and len(supabase.catalog) == 1


def test_a_replay_is_reported_as_unchanged(supabase, caplog):
    with caplog.at_level(logging.INFO):
        send(event())
        send(event())
    assert "item 7: inserted" in caplog.text and "item 7: unchanged" in caplog.text


def test_a_newer_event_replaces_the_row(supabase):
    send(event(status="reserved", write_date="2026-09-20 10:00:00"))
    send(event(status="sold", write_date="2026-09-20 10:05:00"))
    assert supabase.catalog[7]["status"] == "sold" and supabase.catalog[7]["updated_at"] == "2026-09-20T10:05:00+00:00"


def test_an_event_that_arrives_late_does_not_overwrite_a_newer_one(supabase, caplog):
    send(event(status="sold", write_date="2026-09-20 10:05:00"))
    with caplog.at_level(logging.INFO):
        send(event(status="reserved", write_date="2026-09-20 10:00:00"))
    assert supabase.catalog[7]["status"] == "sold" and supabase.catalog[7]["updated_at"] == "2026-09-20T10:05:00+00:00"
    assert "item 7: stale" in caplog.text


def test_different_items_make_different_rows(supabase):
    send(event(id=7))
    send(event(id=8, name="2021 Honda Civic"))
    assert set(supabase.catalog) == {7, 8}


def test_two_simultaneous_deliveries_make_one_row(supabase):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine.test") as ac:
            return await asyncio.gather(*(ac.post("/webhook/odoo-status", json=event(), headers={HEADER: SECRET}) for _ in range(10)))

    responses = asyncio.run(scenario())
    assert {r.status_code for r in responses} == {202}
    assert len(supabase.catalog) == 1 and supabase.catalog[7]["status"] == "sold"


def test_simultaneous_deliveries_from_threads_make_one_row(supabase):
    go = threading.Event()
    codes = []

    def deliver():
        go.wait()
        codes.append(send(event()).status_code)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join()
    assert codes == [202] * 8 and len(supabase.catalog) == 1


def test_the_response_does_not_wait_for_supabase(supabase, monkeypatch):
    """Odoo calls this inside its own write with a short timeout, so the work happens after the response."""
    order = []
    monkeypatch.setattr(odoo_status, "process", lambda e: order.append("processed"))
    send(event())
    assert order == ["processed"]  # TestClient runs background tasks before returning; the route itself only queued it


# Retries

def test_a_transient_failure_is_retried_with_a_growing_pause_and_then_succeeds(supabase, monkeypatch):
    pauses = []
    monkeypatch.setattr(odoo_status.time, "sleep", pauses.append)
    outcomes = iter([httpx.ConnectError("down"), 503, 429])
    original = supabase.handle

    def flaky(request):
        step = next(outcomes, None)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(step)
        return original(request)

    monkeypatch.setattr(supabase, "handle", flaky)
    monkeypatch.setattr(odoo_status, "_new_client", lambda: httpx.Client(transport=httpx.MockTransport(flaky)))
    assert send(event()).status_code == 202
    assert supabase.catalog[7]["status"] == "sold"
    assert pauses == [0.25, 0.5, 1.0]


def test_after_the_last_attempt_the_failure_is_logged_without_the_address_or_key(supabase, monkeypatch, caplog):
    supabase.fail_with = 503
    with caplog.at_level(logging.ERROR):
        assert send(event()).status_code == 202  # Odoo is not told: it cannot do anything with it
    assert len(calls_to(supabase)) == odoo_status.ATTEMPTS
    assert "item 7 was not applied" in caplog.text
    assert "supabase.co" not in caplog.text and "service-key" not in caplog.text and SECRET not in caplog.text


def test_a_network_error_every_time_is_retried_then_given_up(supabase, caplog):
    supabase.fail_with = httpx.ConnectError("no route to proj.supabase.co")
    with caplog.at_level(logging.ERROR):
        send(event())
    assert len(calls_to(supabase)) == odoo_status.ATTEMPTS and "ConnectError" in caplog.text and "proj.supabase" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 404, 409])
def test_a_refusal_that_is_not_transient_is_not_retried(supabase, status):
    supabase.fail_with = status
    send(event())
    assert len(calls_to(supabase)) == 1


def test_without_supabase_configured_the_event_is_dropped_with_a_log_line(supabase, monkeypatch, caplog):
    monkeypatch.delenv("SUPABASE_URL")
    with caplog.at_level(logging.ERROR):
        assert send(event()).status_code == 202
    assert "Supabase is not configured" in caplog.text and supabase.requests == []


def test_an_unexpected_answer_from_the_function_is_not_trusted(supabase, monkeypatch, caplog):
    monkeypatch.setattr(odoo_status, "_new_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json="weird"))))
    with caplog.at_level(logging.ERROR):
        send(event())
    assert "unexpected" in caplog.text


# Nothing sensitive in the log

def test_neither_the_secret_nor_the_event_text_is_logged(supabase, caplog):
    with caplog.at_level(logging.DEBUG):
        send(event(name="Sarah Connor's Camry"))
        send(event(), secret="a wrong guess that is long enough")
    assert SECRET not in caplog.text and "Sarah" not in caplog.text and "wrong guess" not in caplog.text


# Start-up

def test_start_up_says_when_the_route_is_closed(monkeypatch, caplog):
    monkeypatch.delenv("ODOO_WEBHOOK_SECRET", raising=False)

    async def start():
        async with main_module.lifespan(app):
            pass

    with caplog.at_level(logging.WARNING):
        asyncio.run(start())
    assert "ODOO_WEBHOOK_SECRET is not set" in caplog.text


def test_start_up_is_quiet_when_the_secret_is_usable(monkeypatch, caplog):
    monkeypatch.setenv("ODOO_WEBHOOK_SECRET", SECRET)

    async def start():
        async with main_module.lifespan(app):
            pass

    with caplog.at_level(logging.WARNING):
        asyncio.run(start())
    assert "ODOO_WEBHOOK_SECRET" not in caplog.text


# Write times

@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("2026-09-20 10:00:00", "2026-09-20T10:00:00+00:00"),
        ("2026-09-20T10:00:00", "2026-09-20T10:00:00+00:00"),
        ("2026-09-20 10:00:00.250000", "2026-09-20T10:00:00.250000+00:00"),
        ("2026-09-20T10:00:00Z", "2026-09-20T10:00:00+00:00"),
        ("2026-09-20T13:30:00+03:30", "2026-09-20T10:00:00+00:00"),
    ],
)
def test_write_times_are_read_as_utc(given, expected):
    assert odoo_status.parse_write_time(given).isoformat() == expected
