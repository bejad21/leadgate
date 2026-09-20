"""Talk mode: the owner types plain messages and they go to one customer, without replying to an alert each time."""
import datetime as dt

import pytest

from engine import owner_bot
from engine.tests.fakes import lead_row
from engine.tests.test_owner_bot import NOW, OWNER, Recorder, press, say, world  # noqa: F401  (fixtures)


@pytest.fixture
def two(world):
    """Two customers with leads, so it matters which one the owner is talking to."""
    world.store.save_lead_chat(72, chat_id=556, kind="lead", item_name="Civic", customer_name="Omar")
    return world


def talking(world, lead_id=71):
    say(world, f"/talk {lead_id}")


# ---------------------------------------------------------------- starting

def test_talk_starts_a_session_and_pauses_the_assistant_in_that_chat(world):
    talking(world)
    assert world.store.get_talk(now=NOW) == 71
    assert world.store.get_handoff(555, now=NOW)["lead_id"] == 71
    assert "Sarah Connor" in world.rec.owner_texts[-1]["text"] and "/back" in world.rec.owner_texts[-1]["text"]


def test_the_talk_button_does_the_same_as_the_command(world):
    press(world, "k:71")
    assert world.store.get_talk(now=NOW) == 71 and world.store.get_handoff(555, now=NOW)
    assert world.rec.toasts[-1][1]


def test_talking_to_an_unknown_lead_is_refused(world):
    say(world, "/talk 999")
    assert world.store.get_talk(now=NOW) is None
    assert "chat" in world.rec.owner_texts[-1]["text"].lower()


@pytest.mark.parametrize("bad", ["", "abc", "-5", "71x", "7 1"])
def test_a_bad_lead_number_gets_the_usage_line(world, bad):
    say(world, f"/talk {bad}".rstrip())
    assert world.store.get_talk(now=NOW) is None


def test_talk_with_no_number_says_who_you_are_talking_to(world):
    talking(world)
    say(world, "/talk")
    assert "Sarah Connor" in world.rec.owner_texts[-1]["text"]


def test_talk_with_no_number_and_no_session_says_so(world):
    say(world, "/talk")
    assert "nobody" in world.rec.owner_texts[-1]["text"].lower() or "not talking" in world.rec.owner_texts[-1]["text"].lower()


# ---------------------------------------------------------------- plain messages

def test_a_plain_message_goes_to_the_customer_being_talked_to(world):
    talking(world)
    say(world, "Hi Sarah, are you free at 3?")
    assert world.rec.customer == [(555, "Hi Sarah, are you free at 3?")]


def test_several_messages_arrive_in_order_with_no_reply_step(world):
    talking(world)
    for text in ("one", "two", "three"):
        say(world, text)
    assert [t for _, t in world.rec.customer] == ["one", "two", "three"]


def test_a_successful_message_is_not_answered_with_noise(world):
    talking(world)
    count = len(world.rec.owner_texts)
    say(world, "hello")
    assert len(world.rec.owner_texts) == count


def test_each_message_is_logged_on_the_lead_in_odoo(world):
    talking(world)
    say(world, "Can we call at 3?")
    notes = [c for c in world.odoo.calls if c[1] == "message_post"]
    assert notes and "Can we call at 3?" in notes[-1][3]["body"]


def test_each_message_keeps_the_assistant_paused(world):
    talking(world)
    world.store.clear_handoff(555)  # as if it had lapsed
    say(world, "still here")
    assert world.store.get_handoff(555, now=NOW) is not None


def test_a_plain_message_with_no_session_explains_how_to_start_one(world):
    say(world, "hello?")
    text = world.rec.owner_texts[-1]["text"]
    assert "/talk" in text and world.rec.customer == []


def test_blank_messages_are_ignored(world):
    talking(world)
    say(world, "   ")
    assert world.rec.customer == []


def test_an_expired_session_no_longer_relays(world):
    world.store.set_talk(71, until=NOW - dt.timedelta(minutes=1))
    say(world, "hello?")
    assert world.rec.customer == [] and "/talk" in world.rec.owner_texts[-1]["text"]


def test_a_failed_send_tells_the_owner_and_keeps_the_session(world):
    talking(world)
    world.deps.tell_customer = lambda *a: (_ for _ in ()).throw(RuntimeError("blocked the bot"))
    say(world, "hello")
    assert "not accept" in world.rec.owner_texts[-1]["text"].lower()
    assert world.store.get_talk(now=NOW) == 71


def test_a_message_too_long_for_telegram_is_cut_and_the_owner_is_told(world):
    talking(world)
    say(world, "x" * 6000)
    assert len(world.rec.customer[-1][1]) <= owner_bot.RELAY_MAX
    assert "cut" in world.rec.owner_texts[-1]["text"].lower()


def test_talking_to_a_won_lead_is_allowed_but_changes_nothing_in_the_crm(world):
    world.odoo.leads = [lead_row(71, stage="Won", probability=100), lead_row(72)]
    talking(world)
    say(world, "Thanks again for your purchase")
    assert world.rec.customer == [(555, "Thanks again for your purchase")]
    assert not [w for w in world.odoo.written if w[0] == "crm.lead" and "stage_id" in w[2]]
    assert world.rec.statuses == []


# ---------------------------------------------------------------- two customers

def test_switching_customers_sends_to_the_new_one_only(two):
    talking(two, 71)
    say(two, "to Sarah")
    talking(two, 72)
    say(two, "to Omar")
    assert two.rec.customer == [(555, "to Sarah"), (556, "to Omar")]


def test_an_explicit_reply_to_another_customers_alert_wins_and_the_session_stays(two):
    talking(two, 72)
    two.store.add_alert_message(71, 777)
    say(two, "this is for Sarah", reply_to=777)
    assert two.rec.customer == [(555, "this is for Sarah")]
    assert two.store.get_talk(now=NOW) == 72
    say(two, "and this is for Omar")
    assert two.rec.customer[-1] == (556, "and this is for Omar")


def test_a_forwarded_message_from_the_other_customer_does_not_change_the_session(two):
    talking(two, 71)
    two.store.set_handoff(556, 72, until=NOW + dt.timedelta(hours=5))
    owner_bot.forward_customer_message(two.deps, two.store.get_handoff(556, now=NOW), "Omar here")
    say(two, "hi Sarah")
    assert two.rec.customer == [(555, "hi Sarah")]


def test_the_forwarded_message_offers_a_way_to_switch_to_that_customer(two):
    two.store.set_handoff(556, 72, until=NOW + dt.timedelta(hours=5))
    owner_bot.forward_customer_message(two.deps, two.store.get_handoff(556, now=NOW), "Omar here")
    labels = [b["text"] for row in two.rec.owner_html[-1]["buttons"]["inline_keyboard"] for b in row]
    assert "Talk here" in labels


# ---------------------------------------------------------------- ending

def test_back_with_no_number_ends_the_current_session(world):
    talking(world)
    say(world, "/back")
    assert world.store.get_talk(now=NOW) is None and world.store.get_handoff(555, now=NOW) is None
    say(world, "hello?")
    assert world.rec.customer == []


def test_back_with_the_lead_number_also_ends_the_session(world):
    talking(world)
    say(world, "/back 71")
    assert world.store.get_talk(now=NOW) is None


def test_backing_out_of_another_lead_leaves_the_session_alone(two):
    talking(two, 71)
    two.store.set_handoff(556, 72, until=NOW + dt.timedelta(hours=5))
    say(two, "/back 72")
    assert two.store.get_talk(now=NOW) == 71
    assert two.store.get_handoff(556, now=NOW) is None


def test_the_hand_back_button_ends_the_session_too(world):
    talking(world)
    press(world, "b:71")
    assert world.store.get_talk(now=NOW) is None


def test_back_with_no_number_and_no_session_gives_the_usage_line(world):
    say(world, "/back")
    assert "/back" in world.rec.owner_texts[-1]["text"]


# ---------------------------------------------------------------- who may use it

def test_a_stranger_cannot_start_or_use_a_session(world):
    say(world, "/talk 71", chat=999, sender=999)
    assert world.store.get_talk(now=NOW) is None
    talking(world)
    say(world, "let me in", chat=999, sender=999)
    assert world.rec.customer == []


def test_the_help_text_mentions_talk(world):
    say(world, "/help")
    assert "/talk" in world.rec.owner_texts[-1]["text"]
