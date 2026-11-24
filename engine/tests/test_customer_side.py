"""The customer webhook's side of the owner channel: who the customer is, and what it does not ask of them."""
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest
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


# Extracting who wrote

def update(**message):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 555, "type": "private"}, **message}}


def test_the_sender_is_read_from_the_message():
    sender = tg.extract_sender(update(**{"from": {"id": 555, "username": "sarah_c", "first_name": "Sarah"}, "text": "hi"}))
    assert sender == {"id": 555, "username": "sarah_c", "first_name": "Sarah"}


def test_a_sender_without_a_username_has_none():
    sender = tg.extract_sender(update(**{"from": {"id": 555, "first_name": "Sarah"}, "text": "hi"}))
    assert sender is not None and sender["username"] is None


@pytest.mark.parametrize("name", ["x/../y", "ab", "<b>hi</b>", "sarah c", "a" * 40, "évil_name"])
def test_a_hostile_username_is_dropped_at_the_door(name):
    sender = tg.extract_sender(update(**{"from": {"id": 555, "username": name}, "text": "hi"}))
    assert sender is not None and sender["username"] is None


@pytest.mark.parametrize("bad", [{}, {"message": None}, {"message": {}}, {"message": {"from": "x", "chat": {"id": 1}}}, {"message": {"from": {}, "chat": {"id": 1}}}, {"message": {"from": {"id": True}, "chat": {"id": 1}}}])
def test_a_message_with_no_usable_sender_gives_none(bad):
    assert tg.extract_sender(bad) is None


def test_the_chat_id_is_read_from_any_message():
    assert tg.extract_chat_id(update(text="hi")) == 555
    assert tg.extract_chat_id({"message": {"chat": {}}}) is None
    assert tg.extract_chat_id({}) is None


# The webhook

@pytest.fixture(autouse=True)
def wired(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "send_message", MagicMock())
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(1000, 60))
    monkeypatch.setattr(main_module, "_seen_updates", OrderedDict())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 88)))
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


def prompts():
    return [c for c in main_module.send_message.call_args_list if (c.kwargs.get("reply_markup") or {}).get("keyboard")]


# A customer with a username

def test_a_lead_from_a_customer_with_a_username_carries_it_to_the_alert_and_the_store(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "username": "sarah_c", "first_name": "Sarah"})
    assert main_module.deliver_lead_alert.call_args.args[0].username == "sarah_c"
    chat = store_module.get_store().get_lead_chat(71)
    assert chat is not None and chat["username"] == "sarah_c"


def test_a_customer_with_a_username_is_not_asked_for_a_number(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "username": "sarah_c"})
    assert prompts() == []


# What the customer is not asked

def test_a_customer_with_no_username_is_not_asked_for_a_number(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    post("I'll take it", sender={"id": 31, "first_name": "Sarah"})
    assert prompts() == []
    assert main_module.send_message.call_args.args[1] == "ok"


def test_a_failure_in_the_alert_never_costs_the_customer_their_reply(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: lead_turn())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(side_effect=RuntimeError("telegram down")))
    assert post("I'll take it", sender={"id": 31}).status_code == 200
    assert store_module.get_store().get_lead_chat(71) is not None


# A shared contact card is ignored

def test_a_shared_contact_card_is_ignored(monkeypatch):
    ran = MagicMock()
    monkeypatch.setattr(main_module, "run_turn", ran)
    assert post(sender={"id": 31}, contact={"phone_number": "971501234567", "user_id": 31}).status_code == 200
    ran.assert_not_called()
    main_module.send_message.assert_not_called()


def test_a_message_holding_a_number_goes_to_the_assistant_like_any_other(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="thanks"))
    post("my number is 050 123 4567", sender={"id": 31})
    assert main_module.send_message.call_args.args[1] == "thanks"
