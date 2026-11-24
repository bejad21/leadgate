"""Hardening regressions."""
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import odoo_client
from engine.core import actions, followup
from engine.core.crm_contacts import customer_contact
from engine.llm_client import ToolCall
from engine.tests.fakes import FakeOdoo, item
from engine.tests.test_hardening import ActionAdapter, ScriptedLLM

client = TestClient(main_module.app)


# The Odoo client and threads

def test_each_thread_gets_its_own_xmlrpc_connection(monkeypatch):
    """xmlrpc.client's ServerProxy is not thread-safe, and the engine calls Odoo from the request
    loop and a thread pool."""
    class Proxy:
        def __init__(self, url, **kwargs):
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


# Webhook secrets

@pytest.mark.parametrize("path", ["/webhook/telegram", "/webhook/alerts"])
def test_a_non_ascii_secret_is_a_401_not_a_crash(monkeypatch, path):
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "real-secret-0123456789")
    response = client.post(path, json={}, headers={b"X-Telegram-Bot-Api-Secret-Token": "sécret".encode("utf-8")})
    assert response.status_code == 401


def test_a_misconfigured_owner_chat_id_is_reported():
    assert main_module.owner_chat_id_problem("1188563748") is None
    for bad in ("@somebody", "", "-100123456", "0", None, "12.5"):
        assert main_module.owner_chat_id_problem(bad), bad


# A hold whose last write fails

def _reserve(odoo):
    return actions.reserve_item(odoo, "cars", {"item_id": 7, "customer_name": "Sarah", "customer_contact": "sarah@example.com"}, crm_tag="Cars")


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


# Whose contact is believed, more strictly

def test_a_phone_with_a_different_prefix_is_not_the_customers():
    contact = customer_contact("+9995551234567", "call me on 555 1234567")
    assert contact is None or "9995551234567" not in contact


def test_a_local_number_and_its_international_form_are_the_same_person(monkeypatch):
    monkeypatch.setenv("DEFAULT_PHONE_COUNTRY_CODE", "971")
    contact = customer_contact("+971501234567", "call me on 050 123 4567")
    assert contact is not None and "+971501234567" in contact


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


# An item must have been shown

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
    # the refusal sends the model to search first, so an item can still be held in one message after a restart
    assert adapter.executed == [] and "nothing was held" in error and "not been shown" in error and "search for it" in error


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


# A contact that passed is left as written

def test_a_contact_that_already_passes_is_returned_exactly_as_the_model_wrote_it():
    assert customer_contact("555-123-4567", "call me on 555-123-4567") == "555-123-4567"
    assert customer_contact("sarah@example.com 555-123-4567", "sarah@example.com or 555-123-4567") == "sarah@example.com 555-123-4567"


def test_only_the_rejected_part_is_rebuilt():
    result = customer_contact("al@example.com +9995551234567", "I'm al@example.com, call 050 123 4567")
    assert result is not None
    assert "al@example.com" in result and "9995551234567" not in result and "0501234567" in result.replace(" ", "")
