"""The owner's buttons must not undo each other or repeat themselves.

Buttons stay on the screen for ever, a finger can slip, and Telegram can deliver twice, so
every action checks where the lead stands first."""
import datetime as dt

import pytest

from engine import owner_bot
from engine.tests.fakes import item, lead_row
from engine.tests.test_owner_bot import NOW, OWNER, Recorder, press, say, world  # noqa: F401  (fixtures)


def stage_writes(world):
    return [w for w in world.odoo.written if w[0] == "crm.lead" and "stage_id" in w[2]]


def archive_writes(world):
    return [w for w in world.odoo.written if w[0] == "crm.lead" and w[2].get("active") is False]


def won(world, lead_id=71):
    world.odoo.leads = [lead_row(lead_id, stage="Won", probability=100), lead_row(72)]


def archived(world, lead_id=71):
    world.odoo.leads = [lead_row(lead_id, active=False, probability=0), lead_row(72)]


# ---------------------------------------------------------------- a finished lead stays finished

def test_contacted_does_not_reopen_a_won_lead(world):
    won(world)
    press(world, "c:71")
    assert stage_writes(world) == [] and world.rec.statuses == []
    assert "closed" in world.rec.toasts[-1][1].lower()


def test_lost_does_not_archive_a_won_lead(world):
    won(world)
    press(world, "l:71")
    assert archive_writes(world) == [] and world.rec.statuses == []
    assert "won" in world.rec.toasts[-1][1].lower()


def test_lost_on_an_archived_lead_does_nothing_more(world):
    archived(world)
    press(world, "l:71")
    assert archive_writes(world) == [] and world.rec.statuses == []


def test_release_does_not_archive_a_won_lead(world):
    won(world)
    press(world, "x:71")
    assert archive_writes(world) == [] and world.odoo.items[7]["status"] == "reserved"


def test_mark_sold_on_an_archived_lead_is_refused(world):
    archived(world)
    press(world, "s:71")
    assert world.odoo.items[7]["status"] == "reserved" and world.rec.statuses == []


def test_take_on_a_closed_lead_does_nothing(world):
    won(world)
    press(world, "t:71")
    assert world.rec.statuses == [] and not any(w[2].get("user_id") for w in world.odoo.written)


def test_a_button_for_a_lead_that_no_longer_exists_says_so(world):
    world.odoo.leads = []
    press(world, "c:71")
    assert "no longer" in world.rec.toasts[-1][1].lower() and world.rec.statuses == []


# ---------------------------------------------------------------- do not move the stage backwards

def test_contacted_leaves_a_lead_that_is_already_further_along(world):
    world.odoo.leads = [lead_row(71, stage="Proposition"), lead_row(72)]
    press(world, "c:71")
    assert stage_writes(world) == []
    assert world.rec.statuses[-1] == (71, "contacted")


def test_a_reply_does_not_pull_a_won_lead_back_to_qualified(world):
    won(world)
    world.store.add_alert_message(71, 777)
    say(world, "One more thing", reply_to=777)
    assert world.rec.customer == [(555, "One more thing")]  # the owner may still talk to the customer
    assert stage_writes(world) == [] and world.rec.statuses == []


def test_a_reply_moves_only_a_new_lead_to_qualified(world):
    world.store.add_alert_message(71, 777)
    say(world, "Hello", reply_to=777)
    assert stage_writes(world) == [("crm.lead", [71], {"stage_id": 2})]


# ---------------------------------------------------------------- once is enough

def test_confirming_a_viewing_twice_tells_the_customer_once(world):
    press(world, "v:72")
    press(world, "v:72")
    assert len(world.rec.customer) == 1
    assert "already" in world.rec.toasts[-1][1].lower()


def test_a_viewing_on_a_lost_lead_is_not_confirmed(world):
    archived(world, 72)
    press(world, "v:72")
    assert world.rec.customer == []


def test_pressing_contacted_twice_only_records_it_once(world):
    world.odoo.leads = [lead_row(71)]
    press(world, "c:71")
    world.odoo.leads = [lead_row(71, stage="Qualified")]
    press(world, "c:71")
    assert len(stage_writes(world)) == 1


# ---------------------------------------------------------------- a failure half way must not leave a mess

def test_handoff_is_set_even_if_the_odoo_bookkeeping_fails(world):
    world.store.add_alert_message(71, 777)
    world.odoo.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odoo down"))
    say(world, "Hello", reply_to=777)
    assert world.rec.customer == [(555, "Hello")]
    assert world.store.get_handoff(555, now=NOW) is not None
    assert "Sent to" in world.rec.owner_texts[-1]["text"]


def test_a_partial_store_record_is_not_taken_for_a_chat(world):
    world.store._leads.pop(71)
    world.store.add_alert_message(71, 777)
    say(world, "hello", reply_to=777)
    assert world.rec.customer == []
    assert "chat" in world.rec.owner_texts[-1]["text"].lower()


def test_the_right_chat_with_the_wrong_sender_is_ignored_for_messages_too(world):
    world.store.add_alert_message(71, 777)
    say(world, "let me in", reply_to=777, chat=OWNER, sender=999)
    assert world.rec.customer == []


# ---------------------------------------------------------------- the hold survives a missing link

def test_a_hold_whose_lead_link_was_lost_is_still_found_by_the_customer(world):
    world.odoo.items[7].update(reservation_lead_id=0, reserved_for="sarah@example.com")
    world.odoo.leads = [lead_row(71, email_from="sarah@example.com"), lead_row(72)]
    press(world, "x:71")
    assert world.odoo.items[7]["status"] == "available"


# ---------------------------------------------------------------- taking a lead stops the nudge

def test_taking_a_lead_marks_it_so_the_sweeper_leaves_it_alone(world):
    press(world, "t:71")
    tag_writes = [w for w in world.odoo.written if w[0] == "crm.lead" and "tag_ids" in w[2]]
    assert tag_writes and tag_writes[0][2]["tag_ids"][0][0] == 4
    assert any(m == "crm.tag" and v["name"] == "Reminded" for m, v in world.odoo.created)
