"""The web layer around the owner channel: the alert webhook, human mode, and lead bookkeeping."""
import asyncio
import datetime as dt
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import store as store_module
from engine.core.agent_loop import AgentTurnResult
from engine.llm_client import ToolCall
from engine.main import app, config, conversation_history
from engine.notifier import Delivery
from engine.rate_limiter import FixedWindowRateLimiter

client = TestClient(app)
ALERT_SECRET = "alerts-secret"


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "send_message", MagicMock())
    monkeypatch.setattr(main_module, "log_turn", MagicMock())
    monkeypatch.setattr(main_module, "load_history", MagicMock(return_value=[]))
    monkeypatch.setattr(main_module, "_rate_limiter", FixedWindowRateLimiter(1000, 60))
    monkeypatch.setattr(main_module, "_seen_updates", OrderedDict())
    monkeypatch.setattr(main_module, "_seen_owner_updates", OrderedDict())
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", ALERT_SECRET)
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "4242")
    conversation_history.clear()
    yield
    conversation_history.clear()


def post_alert(body, secret=ALERT_SECRET):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return client.post("/webhook/alerts", json=body, headers=headers)


def post_customer(text="hello", chat_id=31, update_id=1):
    body = {"update_id": update_id, "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}, "text": text}}
    return client.post("/webhook/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]})


# ---------------------------------------------------------------- the alert webhook

def test_the_alert_webhook_refuses_everything_when_no_secret_is_set(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", raising=False)
    assert post_alert({"update_id": 1}).status_code == 503


@pytest.mark.parametrize("secret", [None, "", "wrong"])
def test_the_alert_webhook_rejects_a_bad_secret(monkeypatch, secret):
    handler = MagicMock()
    monkeypatch.setattr(main_module.owner_bot, "handle_update", handler)
    assert post_alert({"update_id": 1}, secret=secret).status_code == 401
    handler.assert_not_called()


def test_a_valid_alert_update_is_handled_with_the_owner_dependencies(monkeypatch):
    handler, deps = MagicMock(), object()
    monkeypatch.setattr(main_module.owner_bot, "handle_update", handler)
    monkeypatch.setattr(main_module, "_owner_deps", lambda: deps)
    body = {"update_id": 5, "callback_query": {"id": "x"}}
    assert post_alert(body).status_code == 200
    handler.assert_called_once_with(body, deps)


def test_a_redelivered_alert_update_is_handled_once(monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(main_module.owner_bot, "handle_update", handler)
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    post_alert({"update_id": 9})
    post_alert({"update_id": 9})
    assert handler.call_count == 1


def test_a_failure_while_handling_still_answers_200(monkeypatch):
    monkeypatch.setattr(main_module.owner_bot, "handle_update", MagicMock(side_effect=RuntimeError("x")))
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    assert post_alert({"update_id": 11}).status_code == 200


def test_the_customer_secret_does_not_open_the_alert_webhook():
    assert post_alert({"update_id": 1}, secret=config["TELEGRAM_WEBHOOK_SECRET"]).status_code == 401


# ---------------------------------------------------------------- lead bookkeeping

def _lead_turn(kind_tool="create_lead", result=None):
    return AgentTurnResult(
        reply="ok",
        tool_calls_made=[ToolCall(name=kind_tool, arguments={"name": "Camry", "customer_name": "Sarah", "customer_contact": "s@example.com"})],
        tool_results=[result or {"lead_id": 71}],
    )


def test_a_new_lead_remembers_its_chat_and_its_alert_message(monkeypatch):
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: _lead_turn())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 88)))
    post_customer()
    store = store_module.get_store()
    assert store.get_lead_chat(71)["chat_id"] == 31 and store.lead_for_message(88) == 71
    assert main_module.deliver_lead_alert.call_args.kwargs["can_reply"] is True


def test_if_the_chat_cannot_be_remembered_the_alert_goes_out_without_a_reply_button(monkeypatch):
    broken = MagicMock()
    broken.get_handoff.return_value = None  # only saving fails; the customer is not in human mode
    broken.save_lead_chat.side_effect = RuntimeError("mongo down")
    store_module.set_store(broken)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: _lead_turn())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 88)))
    assert post_customer().status_code == 200
    assert main_module.deliver_lead_alert.call_args.kwargs["can_reply"] is False


def test_a_reservation_is_remembered_with_its_kind(monkeypatch):
    result = {"lead_id": 72, "kind": "reservation", "item_name": "2024 Corolla", "price": 21950.0, "detail": "Held for 24 hours"}
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: _lead_turn("reserve_item", result))
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(True, 90)))
    post_customer()
    chat = store_module.get_store().get_lead_chat(72)
    assert chat["kind"] == "reservation" and chat["detail"] == "Held for 24 hours" and chat["item_name"] == "2024 Corolla"


# ---------------------------------------------------------------- human mode

def _handoff(chat_id=31, hours=5):
    store_module.get_store().set_handoff(chat_id, 71, until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours))


def test_in_human_mode_the_assistant_stays_quiet_and_the_owner_is_told(monkeypatch):
    _handoff()
    ran = MagicMock()
    forwarded = MagicMock()
    monkeypatch.setattr(main_module, "run_turn", ran)
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", forwarded)
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    assert post_customer("Can we do 4pm?").status_code == 200
    ran.assert_not_called()
    main_module.send_message.assert_not_called()
    assert forwarded.call_args.args[2] == "Can we do 4pm?"
    assert 31 not in conversation_history


def test_human_mode_turns_are_logged_as_human_handled_so_history_skips_them(monkeypatch):
    _handoff()
    monkeypatch.setenv("MONGODB_URI", "mongodb://unused")
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", MagicMock())
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    post_customer("hello?")
    assert main_module.log_turn.call_args.kwargs["handled_by"] == "human"


def test_human_mode_is_per_chat(monkeypatch):
    _handoff(chat_id=999)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="hi there"))
    post_customer(chat_id=31)
    main_module.send_message.assert_called_once()


def test_an_expired_handoff_lets_the_assistant_answer_again(monkeypatch):
    _handoff(hours=-1)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="hi again"))
    post_customer()
    assert main_module.send_message.call_args.args[1] == "hi again"


def test_if_the_store_fails_the_assistant_still_answers(monkeypatch):
    broken = MagicMock()
    broken.get_handoff.side_effect = RuntimeError("mongo down")
    store_module.set_store(broken)
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="still here"))
    post_customer()
    assert main_module.send_message.call_args.args[1] == "still here"


def test_a_message_in_human_mode_is_still_screened_before_it_reaches_the_owner(monkeypatch):
    _handoff()
    forwarded = MagicMock()
    monkeypatch.setattr(main_module.owner_bot, "forward_customer_message", forwarded)
    monkeypatch.setattr(main_module, "_owner_deps", lambda: object())
    post_customer("hello\x00‮ world")
    assert "\x00" not in forwarded.call_args.args[2] and "‮" not in forwarded.call_args.args[2]


# ---------------------------------------------------------------- the reminder task

def test_the_reminder_task_only_starts_when_alerts_are_configured(monkeypatch):
    started = []

    async def fake_run(make_deps, minutes, interval=60):
        started.append(minutes)
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module.sweeper, "run_forever", fake_run)
    monkeypatch.setattr(main_module, "LEAD_REMINDER_MINUTES", 30)

    async def run(configured):
        for name in ("TELEGRAM_ALERTS_BOT_TOKEN", "TELEGRAM_ALERTS_CHAT_ID"):
            if configured:
                monkeypatch.setenv(name, "x")
            else:
                monkeypatch.delenv(name, raising=False)
        async with main_module.lifespan(app):
            await asyncio.sleep(0)

    asyncio.run(run(False))
    assert started == []
    asyncio.run(run(True))
    assert started == [30]


def test_a_reminder_setting_of_zero_turns_the_task_off(monkeypatch):
    started = []

    async def fake_run(*a, **k):
        started.append(1)

    monkeypatch.setattr(main_module.sweeper, "run_forever", fake_run)
    monkeypatch.setattr(main_module, "LEAD_REMINDER_MINUTES", 0)
    monkeypatch.setenv("TELEGRAM_ALERTS_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "y")

    async def run():
        async with main_module.lifespan(app):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert started == []
