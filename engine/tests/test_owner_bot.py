"""The owner's side of the conversation: buttons, replies, and human mode."""
import datetime as dt

import pytest

from engine import owner_bot
from engine.core import followup
from engine.store import InMemoryStore
from engine.tests.fakes import FakeOdoo, item, lead_row

OWNER = 4242
NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)


class Recorder:
    """Stands in for the alert bot, the customer bot and the dashboard mirror."""

    def __init__(self):
        self.customer = []      # (chat_id, text)
        self.owner_texts = []   # plain messages to the owner
        self.owner_html = []
        self.toasts = []        # (callback_id, text)
        self.statuses = []      # (lead_id, status)
        self._next_message_id = 500

    # the "owner" surface (what notifier offers)
    def send_owner_message(self, text, *, buttons=None, force_reply=False, reply_to=None):
        self.owner_texts.append({"text": text, "buttons": buttons, "force_reply": force_reply})
        self._next_message_id += 1
        return type("D", (), {"ok": True, "message_id": self._next_message_id})()

    def send_owner_html(self, text, *, buttons=None):
        self.owner_html.append({"text": text, "buttons": buttons})
        self._next_message_id += 1
        return type("D", (), {"ok": True, "message_id": self._next_message_id})()

    def answer_callback(self, callback_id, text=""):
        self.toasts.append((callback_id, text))


@pytest.fixture(autouse=True)
def salesperson(monkeypatch):
    monkeypatch.setenv("ODOO_SALESPERSON_LOGIN", "admin")
    for cache in (followup._user_ids, followup._xmlid_ids, followup._model_ids):
        cache.clear()


@pytest.fixture
def world():
    rec = Recorder()
    odoo = FakeOdoo(items={7: item(7, status="reserved"), 8: item(8)}, leads=[lead_row(71), lead_row(72)])
    odoo.items[7].update(reservation_lead_id=71, reserved_for="sarah@example.com")
    store = InMemoryStore()
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="2024 Toyota Corolla", customer_name="Sarah Connor")
    store.save_lead_chat(72, chat_id=556, kind="viewing", item_name="2024 Toyota Corolla", customer_name="Omar", detail="Sat 26 Sep, afternoon")
    deps = owner_bot.OwnerDeps(
        odoo=odoo, store=store, owner=rec,
        tell_customer=lambda chat_id, text: rec.customer.append((chat_id, text)),
        mirror_status=lambda lead_id, status: rec.statuses.append((lead_id, status)),
        owner_chat_id=OWNER, now=lambda: NOW,
    )
    return type("W", (), {"rec": rec, "odoo": odoo, "store": store, "deps": deps})


def press(world, data, *, chat=OWNER, sender=OWNER, callback_id="cb1", message_id=900):
    update = {"update_id": 1, "callback_query": {"id": callback_id, "from": {"id": sender}, "data": data, "message": {"message_id": message_id, "chat": {"id": chat}}}}
    owner_bot.handle_update(update, world.deps)


def say(world, text, *, reply_to=None, chat=OWNER, sender=OWNER):
    message = {"message_id": 1000, "from": {"id": sender}, "chat": {"id": chat}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    owner_bot.handle_update({"update_id": 2, "message": message}, world.deps)


# ---------------------------------------------------------------- who may act

def test_a_stranger_pressing_a_button_changes_nothing(world):
    press(world, "l:71", chat=999, sender=999)
    assert world.odoo.written == [] and world.rec.statuses == [] and world.rec.toasts == []


def test_a_stranger_cannot_write_to_a_customer(world):
    world.store.add_alert_message(71, 900)
    say(world, "send me your money", reply_to=900, chat=999, sender=999)
    assert world.rec.customer == []


def test_the_right_chat_but_the_wrong_sender_is_ignored(world):
    press(world, "l:71", chat=OWNER, sender=999)
    assert world.rec.statuses == []


def test_junk_updates_are_ignored_without_error(world):
    for update in ({}, {"message": {}}, {"callback_query": {}}, {"edited_message": {"text": "x"}}):
        owner_bot.handle_update(update, world.deps)


# ---------------------------------------------------------------- buttons

def test_take_it_assigns_the_lead_and_says_so(world):
    press(world, "t:71")
    assert ("crm.lead", [71], {"user_id": 2}) in world.odoo.written
    assert world.rec.statuses[-1] == (71, "taken")
    assert world.rec.toasts[-1][0] == "cb1"


def test_contacted_closes_the_task_and_moves_the_lead_on(world):
    world.odoo.activities = [{"id": 31}]
    press(world, "c:71")
    assert ("mail.activity", "action_feedback", [[31]], {"feedback": "Contacted the customer"}) in world.odoo.calls
    assert ("crm.lead", [71], {"stage_id": 2}) in world.odoo.written
    assert world.rec.statuses[-1] == (71, "contacted")


def test_lost_archives_the_lead(world):
    press(world, "l:72")
    assert ("crm.lead", [72], {"active": False, "probability": 0}) in world.odoo.written
    assert world.rec.statuses[-1] == (72, "lost")


def test_mark_sold_sells_the_held_item_and_wins_the_lead(world):
    press(world, "s:71")
    assert world.odoo.items[7]["status"] == "sold"
    assert ("crm.lead", [71], {"stage_id": 4}) in world.odoo.written
    assert world.rec.statuses[-1] == (71, "won")


def test_release_hold_puts_the_item_back_and_closes_the_lead(world):
    press(world, "x:71")
    assert world.odoo.items[7]["status"] == "available"
    assert ("crm.lead", [71], {"active": False, "probability": 0}) in world.odoo.written
    assert world.rec.statuses[-1] == (71, "released")


def test_losing_a_reservation_also_releases_the_hold(world):
    press(world, "l:71")
    assert world.odoo.items[7]["status"] == "available"


def test_confirm_viewing_tells_the_customer(world):
    press(world, "v:72")
    assert world.rec.customer == [(556, "Your viewing of 2024 Toyota Corolla is confirmed for Sat 26 Sep, afternoon. We look forward to seeing you.")]
    assert world.rec.statuses[-1] == (72, "confirmed")


def test_confirming_a_viewing_with_no_stored_chat_still_works_in_odoo(world):
    world.store._leads.pop(72)
    press(world, "v:72")
    assert world.rec.customer == [] and world.rec.statuses[-1] == (72, "confirmed")
    assert "cannot message" in world.rec.toasts[-1][1].lower()


def test_an_unknown_button_gets_a_polite_answer(world):
    press(world, "z:71")
    assert world.rec.toasts[-1][1] and world.rec.statuses == []


@pytest.mark.parametrize("data", ["", "t", "t:", "t:abc", ":71", "t:71:extra"])
def test_malformed_button_data_is_handled(world, data):
    press(world, data)
    assert world.rec.statuses == []


def test_an_odoo_failure_in_a_button_is_reported_not_raised(world):
    world.odoo.write = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odoo down"))
    press(world, "t:71")
    assert "went wrong" in world.rec.toasts[-1][1].lower()


def test_pressing_the_same_button_twice_is_harmless(world):
    press(world, "c:71")
    press(world, "c:71")
    assert world.rec.statuses[-2:] == [(71, "contacted"), (71, "contacted")]


# ---------------------------------------------------------------- reply relay

def test_the_reply_button_opens_a_reply_box_tied_to_that_lead(world):
    press(world, "r:71")
    box = world.rec.owner_texts[-1]
    assert box["force_reply"] is True and "Sarah Connor" in box["text"]


def test_a_reply_to_that_box_reaches_the_customer(world):
    press(world, "r:71")
    box_id = 501
    say(world, "Hi Sarah, this is the showroom. Can we call you at 3?", reply_to=box_id)
    assert world.rec.customer == [(555, "Hi Sarah, this is the showroom. Can we call you at 3?")]


def test_replying_to_the_alert_itself_also_reaches_the_customer(world):
    world.store.add_alert_message(71, 777)
    say(world, "On our way", reply_to=777)
    assert world.rec.customer == [(555, "On our way")]


def test_a_relayed_reply_is_logged_on_the_lead_and_starts_human_mode(world):
    world.store.add_alert_message(71, 777)
    say(world, "Hello there", reply_to=777)
    notes = [c for c in world.odoo.calls if c[1] == "message_post"]
    assert notes and "Hello there" in notes[0][3]["body"]
    handoff = world.store.get_handoff(555, now=NOW)
    assert handoff and handoff["lead_id"] == 71
    assert world.rec.statuses[-1] == (71, "contacted")
    assert "/back 71" in world.rec.owner_texts[-1]["text"]


def test_a_message_that_replies_to_nothing_explains_how(world):
    say(world, "hello?")
    assert "reply" in world.rec.owner_texts[-1]["text"].lower() and world.rec.customer == []


def test_a_reply_to_an_unknown_message_is_not_sent_anywhere(world):
    say(world, "who is this for", reply_to=31337)
    assert world.rec.customer == []


def test_an_empty_relay_is_not_sent(world):
    world.store.add_alert_message(71, 777)
    say(world, "   ", reply_to=777)
    assert world.rec.customer == []


def test_a_reply_for_a_lead_whose_chat_is_unknown_says_so(world):
    world.store._leads.pop(71)
    world.store.add_alert_message(71, 777)
    say(world, "hi", reply_to=777)
    assert world.rec.customer == [] and "chat" in world.rec.owner_texts[-1]["text"].lower()


# ---------------------------------------------------------------- handing back

def test_hand_back_ends_human_mode(world):
    world.store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=5))
    press(world, "b:71")
    assert world.store.get_handoff(555, now=NOW) is None


def test_the_back_command_ends_human_mode(world):
    world.store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=5))
    say(world, "/back 71")
    assert world.store.get_handoff(555, now=NOW) is None


def test_the_back_command_needs_a_lead_number(world):
    say(world, "/back")
    assert "/back" in world.rec.owner_texts[-1]["text"]


# ---------------------------------------------------------------- forwarding while in human mode

def test_a_customer_message_in_human_mode_is_forwarded_to_the_owner(world):
    world.store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=5))
    handoff = world.store.get_handoff(555, now=NOW)
    owner_bot.forward_customer_message(world.deps, handoff, "Can we do 4pm instead?")
    sent = world.rec.owner_html[-1]
    assert "Sarah Connor" in sent["text"] and "Can we do 4pm instead?" in sent["text"]
    assert [b["text"] for row in sent["buttons"]["inline_keyboard"] for b in row] == ["Reply", "Hand back to bot", "Talk here"]


def test_forwarded_customer_text_cannot_inject_markup(world):
    world.store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=5))
    owner_bot.forward_customer_message(world.deps, world.store.get_handoff(555, now=NOW), "<a href='http://evil'>click</a>")
    assert "<a href" not in world.rec.owner_html[-1]["text"]


def test_the_owner_can_reply_straight_to_a_forwarded_message(world):
    world.store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=5))
    owner_bot.forward_customer_message(world.deps, world.store.get_handoff(555, now=NOW), "Hello?")
    forwarded_id = 501  # the first message the recorder hands out
    say(world, "Yes, here", reply_to=forwarded_id)
    assert world.rec.customer[-1] == (555, "Yes, here")


# ---------------------------------------------------------------- commands

def test_help_lists_what_the_owner_can_do(world):
    say(world, "/help")
    text = world.rec.owner_texts[-1]["text"]
    assert "/open" in text and "/back" in text


def test_open_lists_leads_still_waiting(world):
    world.odoo.leads = [{"id": 71, "name": "2024 Toyota Corolla", "contact_name": "Sarah Connor", "create_date": "2026-09-20 11:30:00"}]
    say(world, "/open")
    assert "#71" in world.rec.owner_html[-1]["text"] and "Sarah Connor" in world.rec.owner_html[-1]["text"]


def test_open_with_nothing_waiting_says_so(world):
    world.odoo.leads = []
    say(world, "/open")
    assert "nothing" in world.rec.owner_texts[-1]["text"].lower()
