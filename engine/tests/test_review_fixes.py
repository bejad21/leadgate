"""Fixes that came out of an independent review, each written to fail before the change."""
import threading
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import odoo_client, sweeper
from engine.core import actions, followup
from engine.core.crm_contacts import customer_contact
from engine.llm_client import LLMResponse, ToolCall
from engine.notifier import Delivery
from engine.rate_limiter import FixedWindowRateLimiter
from engine.store import InMemoryStore, MongoStore
from engine.tests.fakes import FakeOdoo, item, lead_row
from engine.tests.test_hardening import ActionAdapter, ScriptedLLM, _history

client = TestClient(main_module.app)


# ---------------------------------------------------------------- the Odoo client and threads

def test_each_thread_gets_its_own_xmlrpc_connection(monkeypatch):
    """xmlrpc.client's ServerProxy is not thread-safe, and the engine calls Odoo from the request
    loop, a thread pool and the background sweeper."""
    class Proxy:
        def __init__(self, url):
            self.url = url

        def authenticate(self, *args):
            return 1

    monkeypatch.setattr(odoo_client.xmlrpc.client, "ServerProxy", Proxy)
    odoo = odoo_client.OdooClient("http://odoo", "db", "u", "p")
    seen = {}
    main_proxy = odoo.models
    assert odoo.models is main_proxy  # stable within a thread
    thread = threading.Thread(target=lambda: seen.setdefault("other", odoo.models))
    thread.start()
    thread.join()
    assert seen["other"] is not main_proxy
    assert main_proxy.url.endswith("/xmlrpc/2/object")


# ---------------------------------------------------------------- webhook secrets

@pytest.mark.parametrize("path", ["/webhook/telegram", "/webhook/alerts"])
def test_a_non_ascii_secret_is_a_401_not_a_crash(monkeypatch, path):
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "real-secret")
    response = client.post(path, json={}, headers={b"X-Telegram-Bot-Api-Secret-Token": "sécret".encode("utf-8")})
    assert response.status_code == 401


def test_a_misconfigured_owner_chat_id_is_reported():
    assert main_module.owner_chat_id_problem("1188563748") is None
    for bad in ("@somebody", "", "-100123456", "0", None, "12.5"):
        assert main_module.owner_chat_id_problem(bad), bad


# ---------------------------------------------------------------- human mode when the forward fails

@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "send_message", MagicMock())
    monkeypatch.setattr(main_module, "log_turn", MagicMock())
    monkeypatch.setattr(main_module, "load_history", MagicMock(return_value=[]))
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(1000, 60))
    monkeypatch.setattr(main_module, "_seen_updates", OrderedDict())
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    main_module.conversation_history.clear()


def _customer_says(text="are you there?"):
    body = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 31, "type": "private"}, "text": text}}
    return client.post("/webhook/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": main_module.config["TELEGRAM_WEBHOOK_SECRET"]})


def _enter_human_mode():
    import datetime as dt
    from engine import store as store_module

    store_module.get_store().set_handoff(31, 71, until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5))


def test_if_the_owner_cannot_be_reached_the_customer_is_told(wired, monkeypatch):
    _enter_human_mode()
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", MagicMock(return_value=Delivery(False)))
    _customer_says()
    assert "pass that on" in main_module.send_message.call_args.args[1]


def test_if_forwarding_raises_the_customer_is_told_too(wired, monkeypatch):
    _enter_human_mode()
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", MagicMock(side_effect=RuntimeError("down")))
    _customer_says()
    assert "pass that on" in main_module.send_message.call_args.args[1]


def test_a_delivered_forward_keeps_the_assistant_silent(wired, monkeypatch):
    _enter_human_mode()
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", MagicMock(return_value=Delivery(True, 5)))
    _customer_says()
    main_module.send_message.assert_not_called()


def test_a_very_long_forwarded_message_is_cut_before_telegram_sees_it():
    from engine import owner_bot
    from engine.tests.test_owner_bot import Recorder

    rec, store = Recorder(), InMemoryStore()
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    deps = owner_bot.OwnerDeps(odoo=FakeOdoo(), store=store, owner=rec, tell_customer=lambda *a: None, mirror_status=lambda *a: None, owner_chat_id=1)
    owner_bot.forward_customer_message(deps, {"lead_id": 71}, "&" * 5000)
    assert len(rec.owner_html[-1]["text"]) < 4096


# ---------------------------------------------------------------- a hold whose last write fails

def _reserve(odoo):
    return actions.reserve_item(odoo, "cars", {"item_id": 7, "customer_name": "Sarah", "customer_contact": "sarah@example.com"})


@pytest.fixture(autouse=True)
def salesperson(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    for cache in (followup._user_ids, followup._xmlid_ids, followup._model_ids):
        cache.clear()


def test_a_failed_link_write_is_retried_once():
    odoo = FakeOdoo(items={7: item(7)})
    real_write, calls = odoo.write, []

    def flaky(model, ids, values):
        calls.append(values)
        if model == "leadgate.catalog.item" and len(calls) == 1:
            raise RuntimeError("blip")
        return real_write(model, ids, values)

    odoo.write = flaky
    result = _reserve(odoo)
    assert "error" not in result and odoo.items[7]["reservation_lead_id"] == result["lead_id"]


def test_if_the_link_cannot_be_written_the_lead_is_still_reported_so_the_owner_hears_of_it():
    odoo = FakeOdoo(items={7: item(7)})
    odoo.write = MagicMock(side_effect=RuntimeError("down"))
    result = _reserve(odoo)
    assert isinstance(result.get("lead_id"), int) and "error" not in result


# ---------------------------------------------------------------- whose contact is believed, more strictly

def test_a_phone_with_a_different_prefix_is_not_the_customers():
    assert customer_contact("+9995551234567", "call me on 555 1234567") is None or "9995551234567" not in customer_contact("+9995551234567", "call me on 555 1234567")


def test_a_local_number_and_its_international_form_are_the_same_person(monkeypatch):
    monkeypatch.setenv("DEFAULT_PHONE_COUNTRY_CODE", "971")
    assert "+971501234567" in customer_contact("+971501234567", "call me on 050 123 4567")


def test_a_shorter_tail_of_a_long_typed_number_is_not_accepted():
    assert customer_contact("1234567", "my number is +971 50 123 4567") != "1234567"


def test_an_email_that_is_only_the_end_of_the_customers_email_is_not_theirs():
    assert customer_contact("e@x.com", "email joe@x.com please") == "joe@x.com"


def test_digits_split_across_two_messages_do_not_join_into_a_number():
    odoo = ActionAdapter()
    history = [{"role": "user", "content": "my number starts 0501234"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "567 thanks, I'm Al"}]
    call = ToolCall(name="create_lead", arguments={"name": "A", "customer_name": "Al", "customer_contact": "+971501234567"})
    from engine.core.agent_loop import run_turn

    run_turn(history, odoo, ScriptedLLM([call]))
    contact = odoo.executed[0][1].get("customer_contact") or ""
    # the customer really typed 0501234 in one message, which is recorded as written; what must
    # not happen is the two messages joining into the phantom number 0501234567
    assert "1234567" not in contact and "+971501234567" not in contact


# ---------------------------------------------------------------- an item must have been shown

def _search_then(write_call, shown_ids):
    """A history in which a search already returned `shown_ids`, then a write call."""
    history = [{"role": "user", "content": "show me cars"}]
    if shown_ids:
        import json

        history += [
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "tool_call_id": "a", "name": "search_inventory", "content": json.dumps({"matches": [{"id": i, "name": "x", "price": 1.0} for i in shown_ids]})},
            {"role": "assistant", "content": "here"},
        ]
    history.append({"role": "user", "content": "hold number 7 for me, I'm Al, al@example.com"})
    adapter = ActionAdapter()
    from engine.core.agent_loop import run_turn

    result = run_turn(history, adapter, ScriptedLLM([write_call]), chat_id=1)
    return adapter, result


def test_an_item_never_shown_in_this_chat_cannot_be_held():
    call = ToolCall(name="reserve_item", arguments={"item_id": 7, "customer_name": "Al", "customer_contact": "al@example.com"})
    adapter, result = _search_then(call, shown_ids=[])
    error = result.tool_results[0]["error"].lower()
    assert adapter.executed == [] and "nothing was held" in error and "confirm" in error


def test_an_item_from_an_earlier_search_can_be_held():
    call = ToolCall(name="reserve_item", arguments={"item_id": 7, "customer_name": "Al", "customer_contact": "al@example.com"})
    adapter, _ = _search_then(call, shown_ids=[3, 7])
    assert [name for name, _ in adapter.executed] == ["reserve_item"]


def test_a_different_item_than_the_ones_shown_is_refused():
    call = ToolCall(name="reserve_item", arguments={"item_id": 99, "customer_name": "Al", "customer_contact": "al@example.com"})
    adapter, result = _search_then(call, shown_ids=[3, 7])
    assert adapter.executed == [] and "error" in result.tool_results[0]


def test_a_plain_lead_needs_no_prior_search():
    call = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Al"})
    adapter, _ = _search_then(call, shown_ids=[])
    assert [name for name, _ in adapter.executed] == ["create_lead"]


# ---------------------------------------------------------------- the sweeper survives faults

class _Owner:
    def __init__(self):
        self.sent = []

    def send_owner_html(self, text, *, buttons=None):
        self.sent.append(text)
        return type("D", (), {"ok": True, "message_id": 9})()


NOW = __import__("datetime").datetime(2026, 9, 20, 12, 0, tzinfo=__import__("datetime").timezone.utc)


def _waiting(lead_id):
    return {"id": lead_id, "name": "x", "contact_name": f"Cust {lead_id}", "email_from": None, "phone": None, "create_date": "2026-09-20 10:00:00"}


def test_a_failing_chat_lookup_still_sends_the_reminder():
    store = MagicMock()
    store.get_lead_chat.side_effect = RuntimeError("mongo down")
    owner = _Owner()
    deps = sweeper.SweepDeps(odoo=FakeOdoo(leads=[_waiting(71)]), store=store, owner=owner, now=lambda: NOW)
    assert sweeper.sweep_once(deps, minutes=30) == 1 and "#71" in owner.sent[0]


def test_one_lead_failing_does_not_stop_the_others():
    odoo = FakeOdoo(leads=[_waiting(71), _waiting(72)])
    real_write = odoo.write

    def picky(model, ids, values):
        if ids == [71]:
            raise RuntimeError("cannot write this one")
        return real_write(model, ids, values)

    odoo.write = picky
    owner = _Owner()
    deps = sweeper.SweepDeps(odoo=odoo, store=InMemoryStore(), owner=owner, now=lambda: NOW)
    assert sweeper.sweep_once(deps, minutes=30) == 1 and any("#72" in t for t in owner.sent)


def test_a_reminder_that_blows_up_after_the_claim_is_unclaimed():
    odoo = FakeOdoo(leads=[_waiting(71)])
    owner = _Owner()
    owner.send_owner_html = MagicMock(side_effect=RuntimeError("boom"))
    deps = sweeper.SweepDeps(odoo=odoo, store=InMemoryStore(), owner=owner, now=lambda: NOW)
    sweeper.sweep_once(deps, minutes=30)
    assert odoo.written[-1][2]["tag_ids"][0][0] == 3


# ---------------------------------------------------------------- the Mongo store

def test_the_store_creates_its_indexes_and_expiries():
    store = MongoStore.__new__(MongoStore)
    store._db = MagicMock()
    store._ensure_indexes()
    leads, handoffs = store._db["lead_chats"], store._db["handoffs"]
    lead_indexes = [c.args[0] for c in leads.create_index.call_args_list]
    assert "lead_id" in lead_indexes and "alert_message_ids" in lead_indexes and "created" in lead_indexes
    assert any(c.kwargs.get("expireAfterSeconds") for c in leads.create_index.call_args_list)
    assert handoffs.create_index.call_args_list[-1].kwargs["expireAfterSeconds"] == 0


def test_a_lead_chat_lookup_ignores_records_that_only_trace_a_message():
    store = MongoStore.__new__(MongoStore)
    store._db = MagicMock()
    store.get_lead_chat(71)
    store._db["lead_chats"].find_one.assert_called_once_with({"lead_id": 71, "chat_id": {"$exists": True}}, {"_id": 0})


def test_an_index_failure_does_not_stop_the_store_working():
    store = MongoStore.__new__(MongoStore)
    store._db = MagicMock()
    store._db["lead_chats"].create_index.side_effect = RuntimeError("no permission")
    store._ensure_indexes()  # must not raise


# ---------------------------------------------------------------- a contact that passed is left as written

def test_a_contact_that_already_passes_is_returned_exactly_as_the_model_wrote_it():
    assert customer_contact("555-123-4567", "call me on 555-123-4567") == "555-123-4567"
    assert customer_contact("sarah@example.com 555-123-4567", "sarah@example.com or 555-123-4567") == "sarah@example.com 555-123-4567"


def test_only_the_rejected_part_is_rebuilt():
    result = customer_contact("al@example.com +9995551234567", "I'm al@example.com, call 050 123 4567")
    assert "al@example.com" in result and "9995551234567" not in result and "0501234567" in result.replace(" ", "")
