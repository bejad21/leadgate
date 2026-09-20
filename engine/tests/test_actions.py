"""Reserving an item and booking a viewing: what the assistant can now do, and what it cannot."""
import datetime as dt

import pytest

from engine.core import actions, followup
from engine.tests.fakes import FakeOdoo, item

TODAY = dt.date(2026, 9, 20)
CONTACT = "sarah@example.com +971 50 123 4567"


@pytest.fixture(autouse=True)
def salesperson(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    for cache in (followup._user_ids, followup._xmlid_ids, followup._model_ids):
        cache.clear()


def _odoo(**kwargs):
    return FakeOdoo(items={7: item(7), 8: item(8, name="2021 Honda Civic", price=15000.0, status="sold"), 9: item(9, name="3bd house", price=350000.0, domain_type="real_estate")}, **kwargs)


def _reserve_args(**extra):
    return {"item_id": 7, "customer_name": "Sarah Connor", "customer_contact": CONTACT, **extra}


def _leads(odoo):
    return [v for m, v in odoo.created if m == "crm.lead"]


# ------------------------------------------------------------------ reservation

def test_an_available_item_is_held_and_a_lead_is_opened():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    assert result["kind"] == "reservation"
    assert isinstance(result["lead_id"], int)
    assert result["item_name"] == "2024 Toyota Corolla"
    assert odoo.items[7]["status"] == "reserved"
    lead = _leads(odoo)[0]
    assert lead["name"] == "Hold: 2024 Toyota Corolla"
    assert lead["contact_name"] == "Sarah Connor" and lead["email_from"] == "sarah@example.com"


def test_the_hold_is_recorded_against_the_customer_and_the_lead():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    reserve_call = [c for c in odoo.calls if c[1] == "action_reserve"][0]
    assert reserve_call[3]["holder"] == "sarah@example.com"
    assert reserve_call[3]["hours"] == actions.HOLD_HOURS
    assert ("leadgate.catalog.item", [7], {"reservation_lead_id": result["lead_id"]}) in odoo.written


def test_the_price_comes_from_the_catalog_never_from_the_model():
    odoo = _odoo()
    actions.reserve_item(odoo, "cars", _reserve_args(price=1))
    assert _leads(odoo)[0]["expected_revenue"] == 21950.0


def test_a_reservation_lead_is_tagged_and_gets_a_same_day_call():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    tags = [v for m, v in odoo.created if m == "crm.tag"]
    assert any(t["name"] == "Reservation" for t in tags)
    task = [v for m, v in odoo.created if m == "mail.activity"][0]
    assert task["res_id"] == result["lead_id"] and "hold" in task["summary"].lower()


def test_an_item_that_is_not_available_is_refused_and_no_lead_is_made():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args(item_id=8))
    assert "error" in result and "no longer available" in result["error"]
    assert _leads(odoo) == []


def test_an_item_from_another_catalog_is_not_found():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args(item_id=9))
    assert "error" in result and _leads(odoo) == []


def test_an_unknown_item_id_is_not_found():
    odoo = _odoo()
    assert "error" in actions.reserve_item(odoo, "cars", _reserve_args(item_id=12345))


def test_a_hold_needs_a_way_to_reach_the_customer():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", {"item_id": 7, "customer_name": "Sarah"})
    assert "error" in result and "phone number or email" in result["error"]
    assert odoo.items[7]["status"] == "available"


@pytest.mark.parametrize("reason", ["hold_limit_reached", "holder_already_has_a_hold", "not_available"])
def test_when_odoo_refuses_the_hold_no_lead_is_left_behind(reason):
    odoo = _odoo()
    odoo.holds_refused_with = reason
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    assert "error" in result and _leads(odoo) == []


def test_if_the_lead_cannot_be_created_the_hold_is_given_back(monkeypatch):
    odoo = _odoo()
    real_create = odoo.create

    def failing(model, values):
        if model == "crm.lead":
            raise RuntimeError("odoo down")
        return real_create(model, values)

    odoo.create = failing
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    assert "error" in result
    assert odoo.items[7]["status"] == "available"


def test_a_resend_by_the_same_customer_returns_the_same_reservation():
    odoo = _odoo()
    first = actions.reserve_item(odoo, "cars", _reserve_args())
    odoo.items[7]["reservation_lead_id"] = first["lead_id"]  # what the write above stored
    again = actions.reserve_item(odoo, "cars", _reserve_args())
    assert again["lead_id"] == first["lead_id"] and again["duplicate"] is True
    assert len(_leads(odoo)) == 1


def test_the_success_message_tells_the_model_not_to_promise_the_item():
    odoo = _odoo()
    result = actions.reserve_item(odoo, "cars", _reserve_args())
    assert "confirm" in result["note"].lower()


# ---------------------------------------------------------------------- viewing

def _view_args(**extra):
    return {"item_id": 7, "customer_name": "Sarah Connor", "customer_contact": CONTACT, "date": "2026-09-26", "slot": "afternoon", **extra}


def test_a_viewing_opens_a_lead_and_a_meeting_on_that_day():
    odoo = _odoo()
    result = actions.book_viewing(odoo, "cars", _view_args(), today=TODAY)
    assert result["kind"] == "viewing"
    assert result["detail"] == "Sat 26 Sep, afternoon"
    assert _leads(odoo)[0]["name"] == "Viewing: 2024 Toyota Corolla"
    task = [v for m, v in odoo.created if m == "mail.activity"][0]
    assert task["date_deadline"] == "2026-09-26" and "afternoon" in task["summary"]
    assert task["activity_type_id"] == 40 + len("mail_activity_data_meeting")


def test_a_viewing_does_not_change_the_items_status():
    odoo = _odoo()
    actions.book_viewing(odoo, "cars", _view_args(), today=TODAY)
    assert odoo.items[7]["status"] == "available"


def test_the_requested_time_is_written_on_the_lead():
    odoo = _odoo()
    actions.book_viewing(odoo, "cars", _view_args(), today=TODAY)
    assert "2026-09-26" in _leads(odoo)[0]["description"] and "afternoon" in _leads(odoo)[0]["description"]


@pytest.mark.parametrize("bad_date", ["2026-09-19", "2027-06-01", "next friday", "26/09/2026", "2026-13-40"])
def test_a_date_that_is_past_too_far_or_not_a_date_is_refused(bad_date):
    odoo = _odoo()
    result = actions.book_viewing(odoo, "cars", _view_args(date=bad_date), today=TODAY)
    assert "error" in result and _leads(odoo) == []


def test_today_is_a_valid_viewing_date():
    assert "error" not in actions.book_viewing(_odoo(), "cars", _view_args(date="2026-09-20"), today=TODAY)


def test_an_unknown_slot_is_refused():
    result = actions.book_viewing(_odoo(), "cars", _view_args(slot="midnight"), today=TODAY)
    assert "error" in result


def test_a_sold_item_cannot_be_viewed():
    result = actions.book_viewing(_odoo(), "cars", _view_args(item_id=8), today=TODAY)
    assert "error" in result


def test_a_viewing_needs_contact_details():
    result = actions.book_viewing(_odoo(), "cars", {"item_id": 7, "customer_name": "Sarah", "date": "2026-09-26", "slot": "morning"}, today=TODAY)
    assert "error" in result and "phone number or email" in result["error"]


def test_asking_twice_for_the_same_viewing_gives_the_same_lead():
    odoo = _odoo()
    first = actions.book_viewing(odoo, "cars", _view_args(), today=TODAY)
    # the fake does not model created leads for the duplicate search, so emulate Odoo finding it
    odoo_search = odoo.search_read
    odoo.search_read = lambda model, domain, fields, **kw: [{"id": first["lead_id"]}] if model == "crm.lead" else odoo_search(model, domain, fields, **kw)
    again = actions.book_viewing(odoo, "cars", _view_args(), today=TODAY)
    assert again["lead_id"] == first["lead_id"] and again.get("duplicate") is True
