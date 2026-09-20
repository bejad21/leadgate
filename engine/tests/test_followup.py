"""What happens to a lead after it exists: who owns it, what is due, what stage it is in."""
import datetime as dt
from unittest.mock import MagicMock

import pytest

from engine.core import followup
from engine.core.adapter_base import create_verified_lead
from engine.tests.fakes import FakeOdoo


@pytest.fixture(autouse=True)
def fresh_cache():
    for cache in (followup._user_ids, followup._xmlid_ids, followup._model_ids):
        cache.clear()
    yield
    for cache in (followup._user_ids, followup._xmlid_ids, followup._model_ids):
        cache.clear()


def test_the_salesperson_is_looked_up_once_by_login(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    odoo = FakeOdoo()
    odoo.search_read = MagicMock(wraps=odoo.search_read)
    assert followup.salesperson_id(odoo) == 2
    assert followup.salesperson_id(odoo) == 2
    assert odoo.search_read.call_count == 1


def test_the_salesperson_defaults_to_the_odoo_login(monkeypatch):
    monkeypatch.delenv("ODOO_SALESPERSON_LOGIN", raising=False)
    monkeypatch.setenv("ODOO_USER", "admin")
    assert followup.salesperson_id(FakeOdoo()) == 2


def test_an_unknown_salesperson_means_no_assignee_not_a_crash(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "nobody")
    assert followup.salesperson_id(FakeOdoo()) is None


def test_a_call_is_created_on_the_lead_for_the_salesperson(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    odoo = FakeOdoo()
    followup.schedule_activity(odoo, 77, summary="Call Sarah about the Corolla", days=1, kind="call", today=dt.date(2026, 9, 20))
    model, values = odoo.created[0]
    assert model == "mail.activity"
    assert values["res_model_id"] == 99 and values["res_id"] == 77
    assert values["date_deadline"] == "2026-09-21"
    assert values["summary"] == "Call Sarah about the Corolla"
    assert values["user_id"] == 2
    assert values["activity_type_id"] == 40 + len("mail_activity_data_call")  # what the fake resolves the "call" type to


def test_a_meeting_activity_can_be_due_on_a_given_date(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    odoo = FakeOdoo()
    followup.schedule_activity(odoo, 5, summary="Viewing", due=dt.date(2026, 9, 26), kind="meeting")
    _, values = odoo.created[0]
    assert values["date_deadline"] == "2026-09-26"
    assert values["activity_type_id"] == 40 + len("mail_activity_data_meeting")


def test_moving_a_lead_to_a_stage_looks_the_stage_up_by_name():
    odoo = FakeOdoo()
    assert followup.set_stage(odoo, 9, "Qualified") is True
    assert odoo.written == [("crm.lead", [9], {"stage_id": 2})]


def test_an_unknown_stage_changes_nothing():
    odoo = FakeOdoo()
    assert followup.set_stage(odoo, 9, "Nonsense") is False
    assert odoo.written == []


def test_marking_a_lead_lost_archives_it_with_zero_probability():
    odoo = FakeOdoo()
    followup.mark_lost(odoo, 9)
    assert odoo.written == [("crm.lead", [9], {"active": False, "probability": 0})]


def test_finishing_the_open_activities_records_what_was_done():
    odoo = FakeOdoo(activities=[{"id": 31}, {"id": 32}])
    followup.complete_activities(odoo, 9, "Contacted the customer")
    assert odoo.calls == [("mail.activity", "action_feedback", [[31, 32]], {"feedback": "Contacted the customer"})]


def test_a_note_is_posted_as_plain_text_and_left_for_odoo_to_escape():
    """Odoo escapes a plain string body itself; escaping first would show &amp;lt; in the chatter."""
    odoo = FakeOdoo()
    followup.post_note(odoo, 9, "<script>alert(1)</script> hi")
    model, method, args, kwargs = odoo.calls[0]
    assert (model, method, args) == ("crm.lead", "message_post", [[9]])
    assert kwargs["body"] == "<script>alert(1)</script> hi"
    assert kwargs["subtype_xmlid"] == "mail.mt_note"


# --- wired into lead creation ------------------------------------------------

def _args(**extra):
    return {"name": "2024 Toyota Corolla", "customer_name": "Sarah Connor", "customer_contact": "sarah@example.com", **extra}


def test_a_new_lead_is_assigned_and_gets_a_follow_up_call(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    odoo = FakeOdoo()
    result = create_verified_lead(odoo, "cars", _args())
    lead_values = [v for m, v in odoo.created if m == "crm.lead"][0]
    assert lead_values["user_id"] == 2
    tasks = [v for m, v in odoo.created if m == "mail.activity"]
    assert len(tasks) == 1 and tasks[0]["res_id"] == result["lead_id"]
    assert "Sarah Connor" in tasks[0]["summary"] and "2024 Toyota Corolla" in tasks[0]["summary"]


def test_a_failed_follow_up_never_costs_the_customer_their_lead(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    odoo = FakeOdoo()
    real_create = odoo.create
    def flaky_create(model, values):
        if model == "mail.activity":
            raise RuntimeError("odoo hiccup")
        return real_create(model, values)
    odoo.create = flaky_create
    result = create_verified_lead(odoo, "cars", _args())
    assert isinstance(result["lead_id"], int)


def test_without_a_known_salesperson_the_lead_is_still_created(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "nobody")
    odoo = FakeOdoo()
    result = create_verified_lead(odoo, "cars", _args())
    lead_values = [v for m, v in odoo.created if m == "crm.lead"][0]
    assert "user_id" not in lead_values and isinstance(result["lead_id"], int)
