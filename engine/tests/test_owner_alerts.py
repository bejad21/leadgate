"""The alert the owner receives, its buttons, and the calls the alert bot makes."""
import json

import httpx
import pytest
import respx

from engine import notifier
from engine.leads import LeadInfo


def lead(**overrides):
    base = dict(
        lead_id=71, domain_type="cars", item_name="2020 Toyota Camry", customer_name="Sarah Connor",
        email="sarah@example.com", phone="+971501112222", price=21834, price_verified=True,
    )
    return LeadInfo(**{**base, **overrides})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERTS_BOT_TOKEN", "alerts-token")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "4242")
    monkeypatch.setenv("ODOO_PUBLIC_URL", "https://odoo.example.test")


def _labels(markup):
    return [[button["text"] for button in row] for row in markup["inline_keyboard"]]


def _data(markup):
    return [button["callback_data"] for row in markup["inline_keyboard"] for button in row]


# ---- wording per kind --------------------------------------------------------------

def test_a_lead_alert_says_new_lead():
    assert notifier.format_alert(lead()).startswith("<b>New lead</b> #71")


def test_a_reservation_alert_says_what_was_held_and_for_how_long():
    text = notifier.format_alert(lead(kind="reservation", detail="Held for 24 hours"))
    assert text.startswith("<b>New reservation</b> #71") and "Held for 24 hours" in text


def test_a_viewing_alert_says_the_requested_day_and_slot():
    text = notifier.format_alert(lead(kind="viewing", detail="Sat 26 Sep, afternoon"))
    assert text.startswith("<b>New viewing request</b> #71") and "Sat 26 Sep, afternoon" in text


def test_the_detail_line_is_escaped():
    assert "<i>" not in notifier.format_alert(lead(kind="viewing", detail="<i>x</i>"))


# ---- buttons -----------------------------------------------------------------------

def test_a_lead_has_take_contacted_reply_and_lost():
    assert _labels(notifier.alert_keyboard(lead())) == [["Take it", "Contacted"], ["Reply", "Lost"]]


def test_a_reservation_can_be_marked_sold_or_released():
    assert _labels(notifier.alert_keyboard(lead(kind="reservation")))[0] == ["Mark sold", "Release hold"]


def test_a_viewing_can_be_confirmed():
    assert "Confirm viewing" in _labels(notifier.alert_keyboard(lead(kind="viewing")))[0]


def test_without_a_stored_chat_there_is_no_reply_button():
    labels = [b for row in _labels(notifier.alert_keyboard(lead(), can_reply=False)) for b in row]
    assert "Reply" not in labels


def test_every_button_carries_the_lead_and_fits_telegrams_limit():
    for kind in ("lead", "reservation", "viewing"):
        for data in _data(notifier.alert_keyboard(lead(kind=kind, lead_id=123456789))):
            assert data.endswith(":123456789") and len(data.encode()) <= 64


# ---- delivery ----------------------------------------------------------------------

@respx.mock
def test_delivery_returns_the_message_id_and_sends_the_buttons(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})
    )
    delivery = notifier.deliver_lead_alert(lead())
    assert delivery.ok is True and delivery.message_id == 88
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "4242" and "inline_keyboard" in body["reply_markup"]


@respx.mock
def test_a_failed_delivery_has_no_message_id(configured):
    respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(return_value=httpx.Response(500))
    delivery = notifier.deliver_lead_alert(lead())
    assert delivery.ok is False and delivery.message_id is None


@respx.mock
def test_the_owner_can_be_sent_a_plain_message(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
    )
    delivery = notifier.send_owner_message("hello <b>", force_reply=True)
    body = json.loads(route.calls[0].request.content)
    assert delivery.message_id == 9 and body["text"] == "hello &lt;b&gt;" and body["reply_markup"] == {"force_reply": True}


@respx.mock
def test_a_button_press_is_acknowledged(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    notifier.answer_callback("cb1", "Done")
    assert json.loads(route.calls[0].request.content) == {"callback_query_id": "cb1", "text": "Done"}


def test_nothing_is_sent_when_the_alert_bot_is_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERTS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERTS_CHAT_ID", raising=False)
    assert notifier.deliver_lead_alert(lead()).ok is False
    assert notifier.send_owner_message("hi").ok is False
