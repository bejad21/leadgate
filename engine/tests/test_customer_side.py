"""The customer webhook's side of talk mode: who the customer is, asking for a number, and using it."""
import json
from collections import OrderedDict
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import store as store_module
from engine import telegram_client as tg
from engine.core.agent_loop import AgentTurnResult
from engine.llm_client import ToolCall
from engine.main import app, config, conversation_history
from engine.notifier import Delivery
from engine.rate_limiter import FixedWindowRateLimiter

client = TestClient(app)


# ---------------------------------------------------------------- extracting who wrote

def update(**message):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 555, "type": "private"}, **message}}


def test_the_sender_is_read_from_the_message():
    sender = tg.extract_sender(update(**{"from": {"id": 555, "username": "sarah_c", "first_name": "Sarah"}, "text": "hi"}))
    assert sender == {"id": 555, "username": "sarah_c", "first_name": "Sarah"}


def test_a_sender_without_a_username_has_none():
    assert tg.extract_sender(update(**{"from": {"id": 555, "first_name": "Sarah"}, "text": "hi"}))["username"] is None


@pytest.mark.parametrize("name", ["x/../y", "ab", "<b>hi</b>", "sarah c", "a" * 40, "évil_name"])
def test_a_hostile_username_is_dropped_at_the_door(name):
    assert tg.extract_sender(update(**{"from": {"id": 555, "username": name}, "text": "hi"}))["username"] is None


@pytest.mark.parametrize("bad", [{}, {"message": None}, {"message": {}}, {"message": {"from": "x", "chat": {"id": 1}}}, {"message": {"from": {}, "chat": {"id": 1}}}, {"message": {"from": {"id": True}, "chat": {"id": 1}}}])
def test_a_message_with_no_usable_sender_gives_none(bad):
    assert tg.extract_sender(bad) is None


def test_the_chat_id_is_read_from_any_message():
    assert tg.extract_chat_id(update(text="hi")) == 555
    assert tg.extract_chat_id({"message": {"chat": {}}}) is None
    assert tg.extract_chat_id({}) is None


def test_a_shared_contact_is_read():
    contact = tg.extract_shared_contact(update(**{"from": {"id": 555}, "contact": {"phone_number": "971501234567", "first_name": "Sarah", "user_id": 555}}))
    assert contact == {"phone_number": "971501234567", "first_name": "Sarah", "user_id": 555}


@pytest.mark.parametrize("bad", [{}, {"message": {"contact": "x"}}, {"message": {"contact": {}}}, {"message": {"contact": {"phone_number": 5}}}, {"message": {"text": "hi"}}])
def test_things_that_are_not_shared_contacts_give_none(bad):
    assert tg.extract_shared_contact(bad) is None


@respx.mock
def test_a_keyboard_is_sent_with_the_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    route = respx.post("https://api.telegram.org/botbot-token/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    tg.send_message(555, "hello", reply_markup={"remove_keyboard": True})
    assert json.loads(route.calls[0].request.content)["reply_markup"] == {"remove_keyboard": True}


@respx.mock
def test_a_keyboard_survives_the_plain_text_retry(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    route = respx.post("https://api.telegram.org/botbot-token/sendMessage").mock(side_effect=[httpx.Response(400), httpx.Response(200, json={"ok": True})])
    tg.send_message(555, "hello <", reply_markup={"remove_keyboard": True})
    assert json.loads(route.calls[1].request.content)["reply_markup"] == {"remove_keyboard": True}


@respx.mock
def test_without_a_keyboard_none_is_sent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    route = respx.post("https://api.telegram.org/botbot-token/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    tg.send_message(555, "hello")
    assert "reply_markup" not in json.loads(route.calls[0].request.content)


# ---------------------------------------------------------------- the webhook

@pytest.fixture(autouse=True)
def wired(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "send_message", MagicMock())
    monkeypatch.setattr(main_module, "log_turn", MagicMock())
    monkeypatch.setattr(main_module, "load_history", MagicMock(return_value=[]))
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(1000, 60))
    monkeypatch.setattr(main_module, "_seen_updates", OrderedDict())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 88)))
    monkeypatch.setattr(main_module, "_share_deps", lambda: "deps")
    conversation_history.clear()
    yield
    conversation_history.clear()


def lead_turn(result=None):
    return AgentTurnResult(
        reply="ok",
        tool_calls_made=[ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sarah", "customer_contact": "s@example.com"})],
        tool_results=[result or {"lead_id": 71}],
    )


def post(text=None, *, sender=None, update_id=1, chat_id=31, **extra):
    message = {"message_id": 1, "chat": {"id": chat_id, "type": "private"}, **extra}
    if text is not None:
        message["text"] = text
    if sender is not None:
        message["from"] = sender
    return client.post("/webhook/telegram", json={"update_id": update_id, "message": message}, headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]})


@pytest.fixture
def real_share(monkeypatch):
    """Working share dependencies (in-memory store, sends recorded on the stubbed customer bot)."""
    from engine import contact_share
    from engine.tests.fakes import FakeOdoo

    deps = contact_share.ShareDeps(
        odoo=FakeOdoo(), store=store_module.get_store(), owner=object(),
        send_customer=lambda chat_id, text, markup=None: main_module.send_message(chat_id, text, reply_markup=markup),
        mirror_phone=lambda *a: None,
    )
    monkeypatch.setattr(main_module, "_share_deps", lambda: deps)
    return deps


def prompts():
    return [c for c in main_module.send_message.call_args_list if (c.kwargs.get("reply_markup") or {}).get("keyboard")]


# ---- a customer with a username

def test_a_lead_from_a_customer_with_a_username_carries_it_to_the_alert_and_the_store(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "username": "sarah_c", "first_name": "Sarah"})
    assert main_module.deliver_lead_alert.call_args.args[0].username == "sarah_c"
    assert store_module.get_store().get_lead_chat(71)["username"] == "sarah_c"


def test_a_customer_with_a_username_is_not_asked_for_a_number(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "username": "sarah_c"})
    assert prompts() == []


# ---- a customer without one

def test_a_customer_with_no_username_is_asked_for_a_number_with_a_share_button(monkeypatch, real_share):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "first_name": "Sarah"})
    [call] = prompts()
    assert call.args[0] == 31 and call.kwargs["reply_markup"]["keyboard"][0][0]["request_contact"] is True
    assert store_module.get_store().has_chat_flag(31, "phone_asked")


def test_the_question_is_not_repeated_for_a_second_lead(monkeypatch, real_share):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("first", sender={"id": 31}, update_id=1)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn({"lead_id": 72}))
    post("second", sender={"id": 31}, update_id=2)
    assert len(prompts()) == 1


def test_a_message_with_no_sender_at_all_still_works_and_is_not_asked(monkeypatch, real_share):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    assert post("I'll take it").status_code == 200
    assert prompts() == []  # with no sender there is no way to know it is a private chat, so nobody is asked


def test_a_failure_asking_for_a_number_never_costs_the_customer_their_reply(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    real = main_module.send_message

    def flaky(chat_id, text, reply_markup=None):
        if reply_markup:
            raise RuntimeError("telegram down")
        return real(chat_id, text)

    monkeypatch.setattr(main_module, "send_message", flaky)
    assert post("I'll take it", sender={"id": 31}).status_code == 200
    assert store_module.get_store().get_lead_chat(71) is not None


def test_no_lead_means_no_question(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="here are cars"))
    post("show me cars", sender={"id": 31})
    assert prompts() == []


# ---- the customer shares a contact

def test_a_shared_contact_is_handled_with_the_sender_and_the_chat(monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", handler)
    contact = {"phone_number": "971501234567", "first_name": "Sarah", "user_id": 31}
    assert post(sender={"id": 31}, contact=contact).status_code == 200
    handler.assert_called_once_with("deps", 31, 31, contact)


def test_a_shared_contact_never_reaches_the_assistant(monkeypatch):
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", MagicMock())
    ran = MagicMock()
    monkeypatch.setattr(main_module, "run_turn", ran)
    post(sender={"id": 31}, contact={"phone_number": "971501234567", "user_id": 31})
    ran.assert_not_called()


def test_a_redelivered_contact_is_handled_once(monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", handler)
    for _ in range(2):
        post(sender={"id": 31}, update_id=7, contact={"phone_number": "971501234567", "user_id": 31})
    assert handler.call_count == 1


def test_a_contact_without_a_sender_is_ignored(monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", handler)
    assert post(contact={"phone_number": "971501234567", "user_id": 31}).status_code == 200
    handler.assert_not_called()


def test_a_contact_is_rate_limited_like_any_message(monkeypatch):
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(0, 60))
    assert post(sender={"id": 31}, contact={"phone_number": "971501234567", "user_id": 31}).status_code == 429


def test_a_failure_handling_a_contact_still_answers_200(monkeypatch):
    monkeypatch.setattr(main_module.contact_share, "handle_shared_contact", MagicMock(side_effect=RuntimeError("boom")))
    assert post(sender={"id": 31}, contact={"phone_number": "971501234567", "user_id": 31}).status_code == 200


def test_the_secret_is_still_required_for_a_contact():
    body = {"update_id": 9, "message": {"chat": {"id": 31}, "from": {"id": 31}, "contact": {"phone_number": "9715", "user_id": 31}}}
    assert client.post("/webhook/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 401


# ---- the customer types a number

def test_a_message_that_holds_a_number_is_offered_to_the_number_handler_and_still_answered(monkeypatch):
    attach = MagicMock(return_value=True)
    monkeypatch.setattr(main_module.contact_share, "maybe_attach_typed_phone", attach)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="thanks"))
    post("my number is 050 123 4567", sender={"id": 31})
    attach.assert_called_once_with("deps", 31, "my number is 050 123 4567")
    assert main_module.send_message.call_args.args[1] == "thanks"


def test_an_ordinary_message_never_touches_the_number_handler(monkeypatch):
    attach = MagicMock()
    monkeypatch.setattr(main_module.contact_share, "maybe_attach_typed_phone", attach)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="hi"))
    post("show me cars under 30000", sender={"id": 31})
    attach.assert_not_called()


def test_a_failure_in_the_number_handler_never_costs_the_reply(monkeypatch):
    monkeypatch.setattr(main_module.contact_share, "maybe_attach_typed_phone", MagicMock(side_effect=RuntimeError("x")))
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="thanks"))
    post("call me on 050 123 4567", sender={"id": 31})
    assert main_module.send_message.call_args.args[1] == "thanks"
