"""What the store must remember for talk mode and for asking a customer for a number."""
import datetime as dt

import pytest

from engine.store import InMemoryStore

NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(hours=5)


@pytest.fixture
def store():
    return InMemoryStore()


# ---------------------------------------------------------------- talk session

def test_there_is_no_talk_session_to_start_with(store):
    assert store.get_talk(now=NOW) is None


def test_a_talk_session_names_the_lead_until_it_expires(store):
    store.set_talk(71, until=LATER)
    assert store.get_talk(now=NOW) == 71
    assert store.get_talk(now=LATER + dt.timedelta(minutes=1)) is None


def test_a_new_talk_session_replaces_the_old_one(store):
    store.set_talk(71, until=LATER)
    store.set_talk(72, until=LATER)
    assert store.get_talk(now=NOW) == 72


def test_a_talk_session_can_be_cleared(store):
    store.set_talk(71, until=LATER)
    store.clear_talk()
    assert store.get_talk(now=NOW) is None


# ---------------------------------------------------------------- username and phone on a lead chat

def test_a_lead_chat_remembers_the_username_and_starts_without_a_phone(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah", username="sarah_c")
    chat = store.get_lead_chat(71)
    assert chat["username"] == "sarah_c" and not chat.get("phone")


def test_a_lead_chat_without_a_username_has_none(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    assert store.get_lead_chat(71)["username"] is None


def test_a_phone_can_be_recorded_against_a_lead(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    store.set_lead_phone(71, "+971501234567")
    assert store.get_lead_chat(71)["phone"] == "+971501234567"


def test_saving_the_chat_again_does_not_forget_the_phone(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    store.set_lead_phone(71, "+971501234567")
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah S")
    assert store.get_lead_chat(71)["phone"] == "+971501234567"


# ---------------------------------------------------------------- the latest lead in a chat

def test_the_latest_lead_of_a_chat_is_the_one_saved_last(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah")
    store.save_lead_chat(72, chat_id=555, kind="viewing", item_name="y", customer_name="Sarah")
    store.save_lead_chat(80, chat_id=999, kind="lead", item_name="z", customer_name="Omar")
    assert store.latest_lead_for_chat(555)["lead_id"] == 72


def test_a_chat_with_no_lead_has_no_latest_lead(store):
    assert store.latest_lead_for_chat(555) is None


# ---------------------------------------------------------------- once-only flags per chat

def test_a_chat_flag_is_off_until_set_and_is_per_chat(store):
    assert store.has_chat_flag(555, "phone_asked") is False
    store.set_chat_flag(555, "phone_asked")
    assert store.has_chat_flag(555, "phone_asked") is True
    assert store.has_chat_flag(556, "phone_asked") is False
    assert store.has_chat_flag(555, "other") is False
