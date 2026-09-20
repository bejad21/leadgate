"""Asking a customer with no public username for a phone number, and using what they send back."""
import pytest

from engine import contact_share
from engine.leads import LeadInfo
from engine.store import InMemoryStore
from engine.tests.fakes import FakeOdoo, lead_row


class Owner:
    """Stands in for the alert bot."""

    def __init__(self):
        self.number_alerts = []

    def deliver_number_alert(self, lead_id, name, phone, username, can_talk=True):
        self.number_alerts.append({"lead_id": lead_id, "name": name, "phone": phone, "username": username, "can_talk": can_talk})
        return type("D", (), {"ok": True, "message_id": 4000 + len(self.number_alerts)})()


class World:
    def __init__(self):
        self.odoo = FakeOdoo(leads=[lead_row(71, phone=None, partner_id=[9, "Sarah"])])
        self.store = InMemoryStore()
        self.owner = Owner()
        self.sent = []      # (chat_id, text, markup)
        self.phones = []    # mirrored to the dashboard
        self.deps = contact_share.ShareDeps(
            odoo=self.odoo, store=self.store, owner=self.owner,
            send_customer=lambda chat_id, text, markup=None: self.sent.append((chat_id, text, markup)),
            mirror_phone=lambda lead_id, phone: self.phones.append((lead_id, phone)),
        )
        self.store.save_lead_chat(71, chat_id=555, kind="lead", item_name="2024 Corolla", customer_name="Sarah Connor")


@pytest.fixture
def w():
    return World()


def lead(**extra):
    base = dict(lead_id=71, domain_type="cars", item_name="x", customer_name="Sarah", email="s@example.com", phone=None, price=1.0, price_verified=True)
    return LeadInfo(**{**base, **extra})


CONTACT = {"phone_number": "971501234567", "first_name": "Sarah", "user_id": 555}


# ---------------------------------------------------------------- normalising a number

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("971501234567", "+971501234567"),       # how Telegram sends a shared contact
        ("+971 50 123 4567", "+971501234567"),
        ("(971) 50-123-4567", "+971501234567"),
        ("00971501234567", "+971501234567"),
        ("050 123 4567", "+971501234567"),        # a local number gets the shop's country code
        ("0501234567", "+971501234567"),
    ],
)
def test_numbers_are_normalised_to_international_form(raw, expected):
    assert contact_share.normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "12345", "1234567890123456", "+", "call me", "12ab34567", "050 123 45 67 89 01 23 45"])
def test_things_that_are_not_phone_numbers_are_rejected(raw):
    assert contact_share.normalise_phone(raw) is None


def test_the_shops_country_code_can_be_changed(monkeypatch):
    monkeypatch.setenv("DEFAULT_PHONE_COUNTRY_CODE", "44")
    assert contact_share.normalise_phone("07911 123456") == "+447911123456"


# ---------------------------------------------------------------- asking

def test_a_customer_with_no_username_and_no_phone_is_asked_once_with_a_share_button(w):
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username=None) is True
    chat_id, text, markup = w.sent[0]
    assert chat_id == 555 and "phone number" in text
    button = markup["keyboard"][0][0]
    assert button["request_contact"] is True and markup["one_time_keyboard"] is True and markup["resize_keyboard"] is True


def test_the_question_is_only_asked_once_per_chat(w):
    contact_share.maybe_ask_for_number(w.deps, 555, lead(), username=None)
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(lead_id=72), username=None) is False
    assert len(w.sent) == 1


def test_a_customer_with_a_username_is_not_asked(w):
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username="sarah_c") is False and w.sent == []


def test_a_customer_who_already_gave_a_number_is_not_asked(w):
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(phone="+971501234567"), username=None) is False and w.sent == []


def test_a_hostile_username_counts_as_no_username(w):
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username="x/../y") is True


def test_a_failed_send_does_not_mark_the_chat_as_asked(w):
    w.deps.send_customer = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))
    assert contact_share.maybe_ask_for_number(w.deps, 555, lead(), username=None) is False
    assert w.store.has_chat_flag(555, "phone_asked") is False


# ---------------------------------------------------------------- a shared contact

def test_a_shared_number_is_added_to_the_lead_the_partner_the_store_and_the_dashboard(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert ("crm.lead", [71], {"phone": "+971501234567"}) in w.odoo.written
    assert ("res.partner", [9], {"phone": "+971501234567"}) in w.odoo.written
    assert w.store.get_lead_chat(71)["phone"] == "+971501234567"
    assert w.phones == [(71, "+971501234567")]
    assert any(c[1] == "message_post" and "+971501234567" in c[3]["body"] for c in w.odoo.calls)


def test_the_owner_is_alerted_with_the_number_and_can_talk_to_the_customer(w):
    w.store.save_lead_chat(71, chat_id=555, kind="lead", item_name="x", customer_name="Sarah Connor", username="sarah_c")
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    alert = w.owner.number_alerts[0]
    assert alert == {"lead_id": 71, "name": "Sarah Connor", "phone": "+971501234567", "username": "sarah_c", "can_talk": True}


def test_replying_to_the_number_alert_reaches_the_customer(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert w.store.lead_for_message(4001) == 71


def test_the_customer_is_thanked_and_the_share_button_is_removed(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    chat_id, text, markup = w.sent[-1]
    assert chat_id == 555 and "number" in text.lower() and markup == {"remove_keyboard": True}


def test_someone_elses_contact_card_is_refused(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact={**CONTACT, "user_id": 999})
    assert w.odoo.written == [] and w.owner.number_alerts == [] and w.phones == []
    assert "your own" in w.sent[-1][1].lower()


@pytest.mark.parametrize("contact", [{"phone_number": "971501234567"}, {"phone_number": "971501234567", "user_id": None}, {"phone_number": "971501234567", "user_id": "555"}])
def test_a_contact_that_does_not_prove_it_is_the_senders_own_is_refused(w, contact):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=contact)
    assert w.odoo.written == []


def test_a_contact_with_a_garbage_number_is_refused(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact={**CONTACT, "phone_number": "not a number"})
    assert w.odoo.written == [] and w.owner.number_alerts == []


def test_a_number_from_a_chat_with_no_lead_is_thanked_but_nothing_is_recorded(w):
    contact_share.handle_shared_contact(w.deps, chat_id=777, sender_id=777, contact={**CONTACT, "user_id": 777})
    assert w.odoo.written == [] and w.owner.number_alerts == [] and w.sent


def test_a_second_share_does_not_overwrite_or_re_alert(w):
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact={**CONTACT, "phone_number": "971509999999"})
    assert len(w.owner.number_alerts) == 1 and w.store.get_lead_chat(71)["phone"] == "+971501234567"


def test_a_lead_that_already_has_a_number_in_odoo_is_left_alone(w):
    w.odoo.leads = [lead_row(71, phone="+971500000000", partner_id=False)]
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert not [x for x in w.odoo.written if x[0] == "crm.lead"] and w.owner.number_alerts == []


def test_an_odoo_failure_does_not_stop_the_owner_hearing_about_it(w):
    w.odoo.write = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odoo down"))
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert len(w.owner.number_alerts) == 1


def test_a_lead_with_no_partner_is_handled(w):
    w.odoo.leads = [lead_row(71, phone=None, partner_id=False)]
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert not [x for x in w.odoo.written if x[0] == "res.partner"] and len(w.owner.number_alerts) == 1


def test_the_number_goes_to_the_customers_latest_lead(w):
    w.store.save_lead_chat(72, chat_id=555, kind="viewing", item_name="y", customer_name="Sarah Connor")
    w.odoo.leads = [lead_row(71, phone=None), lead_row(72, phone=None)]
    contact_share.handle_shared_contact(w.deps, chat_id=555, sender_id=555, contact=CONTACT)
    assert w.owner.number_alerts[0]["lead_id"] == 72


# ---------------------------------------------------------------- a typed number

def asked(w):
    w.store.set_chat_flag(555, "phone_asked")


@pytest.mark.parametrize("text", ["050 123 4567", "my number is +971 50 123 4567", "it's 0501234567 thanks", "+971501234567"])
def test_a_number_typed_after_we_asked_is_used(w, text):
    asked(w)
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, text) is True
    assert w.owner.number_alerts[0]["phone"] == "+971501234567"


def test_a_typed_number_is_ignored_if_we_never_asked(w):
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, "050 123 4567") is False and w.owner.number_alerts == []


@pytest.mark.parametrize("text", ["hello", "I'd pay 1 200 000", "Show me cars under 30000", "the price is 21 950", "", "  "])
def test_ordinary_text_is_not_mistaken_for_a_number(w, text):
    asked(w)
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, text) is False and w.owner.number_alerts == []


def test_a_typed_number_is_ignored_when_the_lead_already_has_one(w):
    asked(w)
    w.store.set_lead_phone(71, "+971500000000")
    assert contact_share.maybe_attach_typed_phone(w.deps, 555, "050 123 4567") is False


def test_a_typed_number_in_a_chat_with_no_lead_is_ignored(w):
    w.store.set_chat_flag(777, "phone_asked")
    assert contact_share.maybe_attach_typed_phone(w.deps, 777, "050 123 4567") is False
