"""Leads that nobody has touched get one more nudge, and only one."""
import datetime as dt

import pytest

from engine import sweeper
from engine.store import InMemoryStore
from engine.tests.fakes import FakeOdoo

NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)


class FakeOwner:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send_owner_html(self, text, *, buttons=None):
        self.sent.append({"text": text, "buttons": buttons})
        return type("D", (), {"ok": self.ok, "message_id": 700 if self.ok else None})()


def waiting_lead(lead_id=71, minutes_ago=45, **extra):
    created = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"id": lead_id, "name": "2024 Toyota Corolla", "contact_name": "Sarah Connor", "email_from": "s@example.com", "phone": "+971501112222", "create_date": created, **extra}


def deps(odoo, owner=None, store=None):
    return sweeper.SweepDeps(odoo=odoo, store=store or InMemoryStore(), owner=owner or FakeOwner(), now=lambda: NOW)


def test_a_lead_nobody_touched_gets_a_reminder():
    odoo, owner = FakeOdoo(leads=[waiting_lead()]), FakeOwner()
    assert sweeper.sweep_once(deps(odoo, owner), minutes=30) == 1
    text = owner.sent[0]["text"]
    assert "#71" in text and "Sarah Connor" in text and "45 min" in text


def test_the_reminder_carries_the_buttons_for_that_lead():
    odoo, owner, store = FakeOdoo(leads=[waiting_lead()]), FakeOwner(), InMemoryStore()
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    sweeper.sweep_once(deps(odoo, owner, store), minutes=30)
    labels = [b["text"] for row in owner.sent[0]["buttons"]["inline_keyboard"] for b in row]
    assert "Contacted" in labels and "Reply" in labels


def test_without_a_stored_chat_the_reply_button_is_left_out():
    odoo, owner = FakeOdoo(leads=[waiting_lead()]), FakeOwner()
    sweeper.sweep_once(deps(odoo, owner), minutes=30)
    labels = [b["text"] for row in owner.sent[0]["buttons"]["inline_keyboard"] for b in row]
    assert "Reply" not in labels


def test_the_lead_is_marked_before_the_reminder_goes_out():
    odoo, owner = FakeOdoo(leads=[waiting_lead()]), FakeOwner()
    order = []
    real_write = odoo.write
    odoo.write = lambda *a, **k: (order.append("marked"), real_write(*a, **k))[1]
    real_send = owner.send_owner_html
    owner.send_owner_html = lambda *a, **k: (order.append("sent"), real_send(*a, **k))[1]
    sweeper.sweep_once(deps(odoo, owner), minutes=30)
    assert order == ["marked", "sent"]


def test_the_marker_is_the_reminded_tag():
    odoo = FakeOdoo(leads=[waiting_lead()])
    sweeper.sweep_once(deps(odoo), minutes=30)
    tag_write = [w for w in odoo.written if w[0] == "crm.lead"][0]
    assert tag_write[1] == [71] and tag_write[2]["tag_ids"][0][0] == 4
    assert any(m == "crm.tag" and v["name"] == "Reminded" for m, v in odoo.created)


def test_a_failed_reminder_is_unmarked_so_the_next_sweep_retries():
    odoo, owner = FakeOdoo(leads=[waiting_lead()]), FakeOwner(ok=False)
    assert sweeper.sweep_once(deps(odoo, owner), minutes=30) == 0
    assert odoo.written[-1][2]["tag_ids"][0][0] == 3


def test_nothing_waiting_sends_nothing():
    odoo, owner = FakeOdoo(leads=[]), FakeOwner()
    assert sweeper.sweep_once(deps(odoo, owner), minutes=30) == 0 and owner.sent == []


def test_the_search_only_asks_for_new_telegram_leads_not_yet_reminded():
    seen = {}
    odoo = FakeOdoo(leads=[])
    real = odoo.search_read

    def spy(model, domain, fields, **kw):
        if model == "crm.lead":
            seen["domain"] = domain
        return real(model, domain, fields, **kw)

    odoo.search_read = spy
    sweeper.sweep_once(deps(odoo), minutes=30)
    flat = [c for c in seen["domain"] if isinstance(c, tuple)]
    assert ("stage_id.name", "=", "New") in flat and ("source_id.name", "=", "Telegram") in flat
    assert any(c[0] == "tag_ids" and c[1] == "not in" for c in flat)
    cutoff = [c for c in flat if c[0] == "create_date"][0]
    assert cutoff[1] == "<" and cutoff[2] == "2026-09-20 11:30:00"


def test_an_odoo_error_is_survived():
    odoo = FakeOdoo()
    odoo.search_read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    assert sweeper.sweep_once(deps(odoo), minutes=30) == 0


def test_the_owner_text_is_escaped():
    odoo, owner = FakeOdoo(leads=[waiting_lead(contact_name="<b>Boss</b>")]), FakeOwner()
    sweeper.sweep_once(deps(odoo, owner), minutes=30)
    assert "<b>Boss</b>" not in owner.sent[0]["text"]
