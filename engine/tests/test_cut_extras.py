"""Features the owner cut stay cut: no talk or human mode, no phone ask, no reminder sweep."""
import importlib
import importlib.util

import pytest

from engine import notifier, owner_bot, telegram_client
from engine.leads import LeadInfo
from engine.store import InMemoryStore
from engine.tests.fakes import FakeOdoo, Recorder


@pytest.mark.parametrize("name", ["engine.contact_share", "engine.sweeper"])
def test_the_cut_modules_are_gone(name):
    assert importlib.util.find_spec(name) is None


def test_the_alert_buttons_are_only_take_contacted_lost_reply_and_the_hold_and_viewing_ones():
    assert set(notifier.ACTIONS) == {"t", "c", "r", "l", "s", "x", "v", "mute"}
    for kind in ("lead", "reservation", "viewing"):
        lead = LeadInfo(lead_id=71, domain_type="cars", item_name="Camry", customer_name="Sarah", email=None, phone=None, price=None, price_verified=True, kind=kind, username="sarah_c")
        codes = {b["callback_data"][0] for row in notifier.alert_keyboard(lead, can_reply=True)["inline_keyboard"] for b in row if "callback_data" in b}
        assert not codes & {"k", "b"}


def test_the_number_alert_helpers_are_gone():
    for name in ("deliver_number_alert", "number_keyboard", "format_number_alert", "whatsapp_link"):
        assert not hasattr(notifier, name)
    assert not hasattr(telegram_client, "extract_shared_contact")
    for name in ("forward_customer_message", "_start_talk", "_hand_back", "HANDOFF_HOURS"):
        assert not hasattr(owner_bot, name)


def _deps():
    rec, store = Recorder(), InMemoryStore()
    store.save_lead_chat(71, chat_id=555, kind="lead", item_name="Camry", customer_name="Sarah")
    deps = owner_bot.OwnerDeps(
        odoo=FakeOdoo(), store=store, owner=rec, tell_customer=lambda chat, text: rec.customer.append((chat, text)),
        mirror_status=lambda *a: None, owner_chat_id=4242,
    )
    return deps, rec


@pytest.mark.parametrize("command", ["/talk 71", "/back 71", "/back"])
def test_talk_and_back_commands_only_show_the_help(command):
    deps, rec = _deps()
    owner_bot.handle_update({"message": {"from": {"id": 4242}, "chat": {"id": 4242}, "text": command}}, deps)
    assert rec.customer == [] and rec.owner_texts[-1]["text"] == owner_bot.HELP
    assert "/talk" not in owner_bot.HELP and "/back" not in owner_bot.HELP


def test_a_plain_message_from_the_owner_is_not_sent_to_anyone():
    deps, rec = _deps()
    owner_bot.handle_update({"message": {"from": {"id": 4242}, "chat": {"id": 4242}, "text": "hello"}}, deps)
    assert rec.customer == []
