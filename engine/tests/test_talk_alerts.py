"""The alert's new buttons and the alert for a customer who shared a phone number."""
import json

import httpx
import pytest
import respx

from engine import notifier
from engine.leads import LeadInfo


def lead(**overrides):
    base = dict(lead_id=71, domain_type="cars", item_name="2020 Toyota Camry", customer_name="Sarah Connor",
                email="sarah@example.com", phone=None, price=21834, price_verified=True)
    return LeadInfo(**{**base, **overrides})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERTS_BOT_TOKEN", "alerts-token")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "4242")
    monkeypatch.setenv("ODOO_PUBLIC_URL", "https://odoo.example.test")


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def labels(markup):
    return [b["text"] for b in buttons(markup)]


# ---------------------------------------------------------------- usernames

@pytest.mark.parametrize("name", ["sarah_c", "Sarah_Connor1", "abcde", "a" * 32])
def test_real_telegram_usernames_are_accepted(name):
    assert notifier.valid_username(name) is True


@pytest.mark.parametrize("name", [None, "", "abc", "1sarah", "sa rah", "sarah!", "sarah/../x", "a" * 33, "sarah?x=1", "sarah\n", "<b>x</b>", "sarah@x", "sárah_c"])
def test_anything_else_is_not_turned_into_a_link(name):
    assert notifier.valid_username(name) is False


def test_the_chat_link_is_a_plain_https_telegram_address():
    assert notifier.chat_link("sarah_c") == "https://t.me/sarah_c"


# ---------------------------------------------------------------- the lead alert

def test_talk_here_is_offered_when_replying_is_possible():
    assert "Talk here" in labels(notifier.alert_keyboard(lead()))


def test_talk_here_carries_the_lead_and_fits_the_limit():
    button = [b for b in buttons(notifier.alert_keyboard(lead(lead_id=123456789))) if b["text"] == "Talk here"][0]
    assert button["callback_data"] == "k:123456789" and len(button["callback_data"].encode()) <= 64


def test_without_a_stored_chat_there_is_no_talk_button():
    assert "Talk here" not in labels(notifier.alert_keyboard(lead(), can_reply=False))


def test_a_known_username_adds_an_open_chat_link_button():
    markup = notifier.alert_keyboard(lead(username="sarah_c"))
    link = [b for b in buttons(markup) if b["text"] == "Open chat"][0]
    assert link == {"text": "Open chat", "url": "https://t.me/sarah_c"}


def test_no_username_means_no_open_chat_button():
    assert "Open chat" not in labels(notifier.alert_keyboard(lead()))


def test_a_hostile_username_never_becomes_a_link():
    assert "Open chat" not in labels(notifier.alert_keyboard(lead(username="x/../../evil")))


def test_the_open_chat_link_works_even_without_a_stored_chat():
    """The owner can still open the customer's Telegram profile even if replying through the bot is impossible."""
    assert "Open chat" in labels(notifier.alert_keyboard(lead(username="sarah_c"), can_reply=False))


def test_the_username_is_shown_in_the_alert_text_and_escaped():
    assert "@sarah_c" in notifier.format_alert(lead(username="sarah_c"))
    assert "@" not in notifier.format_alert(lead()).replace("sarah@example.com", "")


def test_a_hostile_username_is_not_printed_either():
    assert "evil" not in notifier.format_alert(lead(username="<b>evil</b>"))


# ---------------------------------------------------------------- a customer who shared a number

def test_a_whatsapp_link_uses_digits_only():
    assert notifier.whatsapp_link("+971 50 123-4567") == "https://wa.me/971501234567"


def test_the_shared_number_alert_names_the_customer_and_the_number():
    text = notifier.format_number_alert(71, "Sarah <b>C</b>", "+971501234567", None)
    assert "#71" in text and "+971501234567" in text and "<b>C</b>" not in text


def test_the_shared_number_alert_offers_talk_whatsapp_and_the_profile():
    markup = notifier.number_keyboard(71, "+971501234567", "sarah_c", can_talk=True)
    assert labels(markup) == ["Talk here", "WhatsApp", "Open chat"]
    urls = {b["text"]: b["url"] for b in buttons(markup) if "url" in b}
    assert urls == {"WhatsApp": "https://wa.me/971501234567", "Open chat": "https://t.me/sarah_c"}


def test_the_shared_number_alert_without_a_username_still_has_whatsapp():
    assert labels(notifier.number_keyboard(71, "+971501234567", None, can_talk=True)) == ["Talk here", "WhatsApp"]


@respx.mock
def test_the_shared_number_alert_is_delivered_with_its_buttons(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 33}})
    )
    delivery = notifier.deliver_number_alert(71, "Sarah", "+971501234567", "sarah_c", can_talk=True)
    body = json.loads(route.calls[0].request.content)
    assert delivery.ok and delivery.message_id == 33
    assert "+971501234567" in body["text"] and "inline_keyboard" in body["reply_markup"]
