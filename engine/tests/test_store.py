"""The small amount of state the owner channel needs: chats behind leads, and human mode."""
import datetime as dt

import pytest

from engine.store import InMemoryStore

NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def store():
    return InMemoryStore()


def test_a_lead_remembers_which_chat_it_came_from(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="Camry", customer_name="Sarah")
    chat = store.get_lead_chat(71)
    assert chat["chat_id"] == 555 and chat["customer_name"] == "Sarah" and chat["kind"] == "lead"


def test_an_unknown_lead_has_no_chat(store):
    assert store.get_lead_chat(1) is None


def test_saving_a_lead_twice_keeps_one_record(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="Camry", customer_name="Sarah")
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="Camry", customer_name="Sarah S")
    assert store.get_lead_chat(71)["customer_name"] == "Sarah S"


def test_an_alert_message_maps_back_to_its_lead(store):
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="Camry", customer_name="Sarah")
    store.add_alert_message(71, 9001)
    store.add_alert_message(71, 9002)
    assert store.lead_for_message(9001) == 71 and store.lead_for_message(9002) == 71
    assert store.lead_for_message(1234) is None


def test_a_message_for_an_unsaved_lead_is_still_traceable(store):
    store.add_alert_message(99, 5)
    assert store.lead_for_message(5) == 99


def test_a_handoff_is_active_until_it_expires(store):
    store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=6))
    assert store.get_handoff(555, now=NOW)["lead_id"] == 71
    assert store.get_handoff(555, now=NOW + dt.timedelta(hours=7)) is None


def test_a_handoff_can_be_ended_early(store):
    store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=6))
    store.clear_handoff(555)
    assert store.get_handoff(555, now=NOW) is None


def test_handoffs_are_per_chat(store):
    store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=6))
    assert store.get_handoff(556, now=NOW) is None


def test_a_new_handoff_replaces_the_old_one_for_that_chat(store):
    store.set_handoff(555, 71, until=NOW + dt.timedelta(hours=1))
    store.set_handoff(555, 72, until=NOW + dt.timedelta(hours=6))
    assert store.get_handoff(555, now=NOW)["lead_id"] == 72
