"""Fixes from an independent review of talk mode and the phone-number flow, each written to fail first."""
import json
from collections import OrderedDict
from unittest.mock import MagicMock

import httpx
import pytest
import respx

import engine.main as main_module
from engine import contact_share, owner_bot
from engine import store as store_module
from engine import telegram_client as tg
from engine.leads import LeadInfo
from engine.notifier import Delivery
from engine.rate_limiter import FixedWindowRateLimiter
from engine.store import InMemoryStore, MongoStore
from engine.tests.fakes import FakeOdoo, lead_row
from engine.tests.test_contact_share import CONTACT, World, lead
from engine.tests.test_owner_bot import NOW, press, say, world  # noqa: F401  (fixtures)


# ---------------------------------------------------------------- a rejected send must not look like success

@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")


URL = "https://api.telegram.org/botbot-token/sendMessage"


@respx.mock
def test_a_customer_who_blocked_the_bot_raises_instead_of_looking_delivered(token):
    respx.post(URL).mock(return_value=httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    with pytest.raises(httpx.HTTPStatusError):
        tg.send_message(555, "hello", strict=True)
    assert tg.send_message(555, "hello").status_code == 403  # the default still just returns it


@respx.mock
def test_a_formatting_rejection_is_retried_as_plain_text_and_succeeds(token):
    route = respx.post(URL).mock(side_effect=[httpx.Response(400), httpx.Response(200, json={"ok": True})])
    tg.send_message(555, "x" * 3500, strict=True)
    assert json.loads(route.calls[1].request.content)["text"] == "x" * 3500  # the retry is the raw text, well under 4096


@respx.mock
def test_if_the_plain_retry_is_also_rejected_it_raises(token):
    respx.post(URL).mock(return_value=httpx.Response(400))
    with pytest.raises(httpx.HTTPStatusError):
        tg.send_message(555, "hello", strict=True)


@respx.mock
def test_a_successful_send_does_not_raise(token):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert tg.send_message(555, "hello").status_code == 200


def test_a_prompt_the_customer_never_received_is_not_remembered_as_asked():
    w = World()
    w.deps.send_customer = lambda *a, **k: (_ for _ in ()).throw(httpx.HTTPStatusError("403", request=None, response=None))
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username=None) is False
    assert w.store.has_chat_flag(555, "phone_asked") is False


# ---------------------------------------------------------------- dates and times are not phone numbers

@pytest.mark.parametrize(
    "text",
    [
        "viewing on 2026-09-25 14:00",
        "25/09/2026 at 14:30 please",
        "on 2026-09-25",
        "25.09.2026",
        "2026 09 25 works",
        "5551234567",              # ten digits with no + or leading zero is not treated as a phone
        "order 20260925142",       # a long bare number with no phone shape
    ],
)
def test_dates_and_bare_digit_runs_are_not_taken_for_a_number(text):
    w = World()
    w.store.set_chat_flag(555, "phone_asked")
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, text) is False and w.owner.number_alerts == []


@pytest.mark.parametrize("text", ["+971 50 123 4567", "0501234567", "call 050 123 4567 tomorrow 2026-09-25", "971501234567", "00971501234567"])
def test_real_numbers_are_still_taken_even_next_to_a_date(text):
    w = World()
    w.store.set_chat_flag(555, "phone_asked")
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, text) is True
    assert w.owner.number_alerts[0]["phone"] == "+971501234567"


# ---------------------------------------------------------------- only private chats

@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "send_message", MagicMock())
    monkeypatch.setattr(main_module, "log_turn", MagicMock())
    monkeypatch.setattr(main_module, "load_history", MagicMock(return_value=[]))
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(1000, 60))
    monkeypatch.setattr(main_module, "_seen_updates", OrderedDict())
    monkeypatch.setattr(main_module, "_share_deps", lambda: "deps")
    main_module.conversation_history.clear()


def _post(text=None, *, chat_id, sender_id, contact=None, update_id=1):
    from fastapi.testclient import TestClient

    message = {"message_id": 1, "chat": {"id": chat_id, "type": "supergroup" if chat_id != sender_id else "private"}, "from": {"id": sender_id}}
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    return TestClient(main_module.app).post("/webhook/telegram", json={"update_id": update_id, "message": message}, headers={"X-Telegram-Bot-Api-Secret-Token": main_module.config["TELEGRAM_WEBHOOK_SECRET"]})


def test_a_contact_card_from_a_group_is_ignored(wired, monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", handler)
    _post(chat_id=-100123, sender_id=31, contact={"phone_number": "971501234567", "user_id": 31})
    handler.assert_not_called()


def test_a_contact_card_from_a_private_chat_is_handled(wired, monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", handler)
    _post(chat_id=31, sender_id=31, contact={"phone_number": "971501234567", "user_id": 31})
    handler.assert_called_once()


def test_a_number_typed_in_a_group_is_not_used(wired, monkeypatch):
    attach = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "maybe_attach_typed_phone", attach)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: main_module.AgentTurnResult(reply="ok"))
    _post("call me on 050 123 4567", chat_id=-100123, sender_id=31)
    attach.assert_not_called()


def test_a_group_is_never_asked_for_a_number(wired, monkeypatch):
    from engine.core.agent_loop import AgentTurnResult
    from engine.llm_client import ToolCall

    ask = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "maybe_ask_for_number", ask)
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 1)))
    turn = AgentTurnResult(reply="ok", tool_calls_made=[ToolCall(name="create_lead", arguments={"name": "x", "customer_name": "S"})], tool_results=[{"lead_id": 71}])
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: turn)
    _post("I'll take it", chat_id=-100123, sender_id=31)
    ask.assert_not_called()


# ---------------------------------------------------------------- handing back one lead of a chat with two

def test_handing_back_any_lead_of_the_talked_to_chat_ends_the_session(world):
    world.store.save_lead_chat(72, chat_id=555, kind="viewing", item_name="y", customer_name="Sarah Connor")  # a second lead, same chat
    say(world, "/talk 71")
    say(world, "/back 72")
    assert world.store.get_talk(now=NOW) is None and world.store.get_handoff(555, now=NOW) is None
    say(world, "hello?")
    assert world.rec.customer == []


def test_handing_back_another_customer_still_leaves_the_session_alone(world):
    world.store.save_lead_chat(80, chat_id=556, kind="lead", item_name="z", customer_name="Omar")
    say(world, "/talk 71")
    say(world, "/back 80")
    assert world.store.get_talk(now=NOW) == 71


# ---------------------------------------------------------------- a number that Odoo did not accept

def test_a_number_odoo_did_not_take_is_not_remembered_so_a_retry_works():
    w = World()
    real_write = w.odoo.write
    w.odoo.write = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odoo down"))
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert w.store.get_lead_chat(71).get("phone") in (None, "") and len(w.owner.number_alerts) == 1  # the owner still gets it
    w.odoo.write = real_write
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert ("crm.lead", [71], {"phone": "+971501234567"}) in w.odoo.written


def test_a_partner_that_already_has_a_phone_keeps_it():
    w = World()
    real = w.odoo.search_read

    def with_partner_phone(model, domain, fields, **kw):
        if model == "res.partner":
            return [{"id": 9, "phone": "+971500000000"}]
        return real(model, domain, fields, **kw)

    w.odoo.search_read = with_partner_phone
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert not [x for x in w.odoo.written if x[0] == "res.partner"]
    assert ("crm.lead", [71], {"phone": "+971501234567"}) in w.odoo.written


# ---------------------------------------------------------------- a second lead from the same chat

def test_a_second_lead_in_a_chat_that_shared_a_number_starts_with_it():
    w = World()
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    w.store.save_lead_chat(72, chat_id=555, kind="viewing", item_name="y", customer_name="Sarah Connor")
    w.odoo.leads = [lead_row(71, phone="+971501234567"), lead_row(72, phone=None)]
    second = lead(lead_id=72)
    assert contact_share.inherit_known_number(w.deps, 555, second) == "+971501234567"
    assert ("crm.lead", [72], {"phone": "+971501234567"}) in w.odoo.written
    assert w.store.get_lead_chat(72)["phone"] == "+971501234567"


def test_a_lead_that_already_has_a_number_inherits_nothing():
    w = World()
    w.store.set_lead_phone(71, "+971501234567")
    assert contact_share.inherit_known_number(w.deps, 555, lead(phone="+971509999999")) is None


def test_a_chat_that_never_shared_a_number_inherits_nothing():
    w = World()
    assert contact_share.inherit_known_number(w.deps, 555, lead(lead_id=72)) is None


def test_an_odoo_failure_while_inheriting_is_survived():
    w = World()
    w.store.set_lead_phone(71, "+971501234567")
    w.odoo.write = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    assert contact_share.inherit_known_number(w.deps, 555, lead(lead_id=72)) is None


# ---------------------------------------------------------------- asking is claimed before it is sent

def test_two_racing_questions_send_only_one_prompt():
    w = World()
    assert w.store.claim_chat_flag(555, "phone_asked") is True
    assert w.store.claim_chat_flag(555, "phone_asked") is False
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username=None) is False and w.sent == []


def test_a_claimed_flag_can_be_released():
    store = InMemoryStore()
    store.claim_chat_flag(555, "phone_asked")
    store.clear_chat_flag(555, "phone_asked")
    assert store.has_chat_flag(555, "phone_asked") is False


# ---------------------------------------------------------------- the share button stays until it is used

@pytest.mark.parametrize("contact", [{**CONTACT, "user_id": 999}, {**CONTACT, "phone_number": "hello"}])
def test_a_refused_share_offers_the_button_again(contact):
    w = World()
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=contact)
    assert w.sent[-1][2] == contact_share.CONTACT_KEYBOARD


# ---------------------------------------------------------------- the Mongo store

def mongo():
    store = MongoStore.__new__(MongoStore)
    store._db = MagicMock()
    return store


def test_the_latest_lead_of_a_chat_is_sorted_newest_first_with_a_tie_breaker():
    store = mongo()
    store._db["lead_chats"].find.return_value.sort.return_value.limit.return_value = iter([{"lead_id": 72}])
    assert store.latest_lead_for_chat(555) == {"lead_id": 72}
    store._db["lead_chats"].find.assert_called_once_with({"chat_id": 555}, {"_id": 0})
    assert store._db["lead_chats"].find.return_value.sort.call_args.args[0] == [("created", -1), ("lead_id", -1)]


def test_chat_lookups_are_indexed():
    store = mongo()
    store._ensure_indexes()
    indexed = [c.args[0] for c in store._db["lead_chats"].create_index.call_args_list]
    assert "chat_id" in indexed


def test_claiming_a_flag_in_mongo_reports_whether_it_was_new():
    store = mongo()
    store._db["chat_flags"].update_one.return_value.upserted_id = "abc"
    assert store.claim_chat_flag(555, "phone_asked") is True
    store._db["chat_flags"].update_one.return_value.upserted_id = None
    assert store.claim_chat_flag(555, "phone_asked") is False


def test_a_naive_mongo_talk_expiry_is_read_as_utc():
    import datetime as dt

    store = mongo()
    store._db["owner_state"].find_one.return_value = {"lead_id": 71, "until": dt.datetime(2026, 9, 21, 20, 0)}  # naive, as Mongo returns
    assert store.get_talk(now=dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc)) == 71
    assert store.get_talk(now=dt.datetime(2026, 9, 21, 21, 0, tzinfo=dt.timezone.utc)) is None


def test_the_phone_known_for_a_chat_comes_from_any_of_its_leads():
    store = mongo()
    store._db["lead_chats"].find_one.return_value = {"phone": "+971501234567"}
    assert store.phone_for_chat(555) == "+971501234567"
    store._db["lead_chats"].find_one.return_value = None
    assert store.phone_for_chat(555) is None


# ---------------------------------------------------------------- a viewing confirmation the customer never got

def test_a_viewing_confirmation_telegram_rejects_is_not_marked_confirmed(world):
    world.deps.tell_customer = lambda *a: (_ for _ in ()).throw(httpx.HTTPStatusError("403", request=None, response=None))
    press(world, "v:72")
    assert world.store.has_flag(72, "viewing_confirmed") is False
    assert any("not accept" in m["text"] for m in world.rec.owner_texts) or any("not accept" in t for _, t in world.rec.toasts)
