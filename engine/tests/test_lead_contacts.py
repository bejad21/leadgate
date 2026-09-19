"""A lead should carry a real contact, not a paragraph of text.

Uses a fake Odoo that records every call, so the tests can say exactly which
records were searched for, created and linked.
"""
import pytest

from engine.core.adapter_base import create_verified_lead
from engine.core.crm_contacts import NAME_MAX, parse_contact


class FakeOdoo:
    def __init__(self, partners=None, sources=None, tags=None, existing_leads=None, fail=()):
        self.partners = partners or []
        self.sources = sources or []
        self.tags = tags or []
        self.existing_leads = existing_leads or []
        self.fail = set(fail)
        self.searches = []
        self.created = []
        self._next = 100

    def search_read(self, model, domain, fields):
        self.searches.append((model, domain))
        if model == "leadgate.catalog.item":
            return [{"id": 1}]
        if model == "res.partner":
            clauses = [c for c in domain if isinstance(c, tuple)]
            emails = [v for f, op, v in clauses if f == "email_normalized"]
            phones = [v for f, op, v in clauses if f in ("phone", "phone_sanitized")]
            return [
                dict(p)
                for p in self.partners
                if (emails and p.get("email", "").lower() == emails[0]) or (phones and p.get("phone") in phones)
            ]
        if model == "utm.source":
            return list(self.sources)
        if model == "crm.tag":
            return list(self.tags)
        if model == "crm.lead":
            return list(self.existing_leads)
        return []

    def create(self, model, values):
        if model in self.fail:
            raise RuntimeError(f"cannot create {model}")
        self._next += 1
        self.created.append((model, values, self._next))
        return self._next

    def lead(self):
        return [v for m, v, _ in self.created if m == "crm.lead"][0]

    def made(self, model):
        return [v for m, v, _ in self.created if m == model]

    def id_of(self, model):
        return [i for m, _, i in self.created if m == model][0]


def make(odoo, **overrides):
    args = {"name": "2020 Toyota Camry", "customer_name": "Sarah Connor", "customer_contact": "sarah.connor@example.com"}
    args.update(overrides)
    return create_verified_lead(odoo, "cars", args)


# ---- parsing ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, email, phone",
    [
        ("sarah@example.com", "sarah@example.com", None),
        ("+971 50 123 4567", None, "+971501234567"),
        ("call 0501234567 or sam@example.co.uk", "sam@example.co.uk", "0501234567"),
        ("(555) 123-4567", None, "5551234567"),
        ("%@gmail.com", None, None),
        ("my email is %@gmail.com", None, None),
        ("a@b.c", None, None),
        ("2020-01-15", None, None),
        ("born 15/01/2020", None, None),
        ("no contact here", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_parse_contact(text, email, phone):
    assert parse_contact(text) == (email, phone)


# ---- the partner -----------------------------------------------------------------

def test_email_creates_a_partner_and_links_the_lead():
    odoo = FakeOdoo()
    result = make(odoo)

    assert odoo.made("res.partner")[0] == {"name": "Sarah Connor", "email": "sarah.connor@example.com"}
    lead = odoo.lead()
    assert lead["partner_id"] == odoo.id_of("res.partner")
    assert lead["contact_name"] == "Sarah Connor"
    assert lead["email_from"] == "sarah.connor@example.com"
    assert "phone" not in lead
    assert result["lead_id"] == odoo.id_of("crm.lead")


def test_phone_number_is_stored_as_a_phone():
    odoo = FakeOdoo()
    make(odoo, customer_contact="+971 50 123 4567")

    lead = odoo.lead()
    assert lead["phone"] == "+971501234567"
    assert "email_from" not in lead
    assert odoo.made("res.partner")[0]["phone"] == "+971501234567"


def test_email_and_phone_in_one_message_are_both_kept():
    odoo = FakeOdoo()
    make(odoo, customer_contact="sam@example.com or call 0501234567")

    lead = odoo.lead()
    assert lead["email_from"] == "sam@example.com"
    assert lead["phone"] == "0501234567"


def test_an_existing_partner_with_the_same_email_is_reused_ignoring_case():
    odoo = FakeOdoo(partners=[{"id": 7, "name": "Sarah Connor", "email": "Sarah.Connor@example.com"}])
    make(odoo)

    assert odoo.made("res.partner") == []
    assert odoo.lead()["partner_id"] == 7


def test_the_partner_is_matched_on_the_normalised_email_never_a_wildcard():
    odoo = FakeOdoo()
    make(odoo)
    searches = [d for m, d in odoo.searches if m == "res.partner"]
    assert searches == [[("email_normalized", "=", "sarah.connor@example.com")]]


def test_an_underscore_in_an_email_does_not_match_a_different_partner():
    stranger = {"id": 7, "name": "Someone Else", "email": "axb@example.com"}
    odoo = FakeOdoo(partners=[stranger])
    make(odoo, customer_contact="a_b@example.com")

    assert odoo.lead()["partner_id"] != 7
    assert len(odoo.made("res.partner")) == 1


def test_a_percent_sign_cannot_be_used_to_grab_someone_elses_partner():
    odoo = FakeOdoo(partners=[{"id": 7, "name": "Someone Else", "email": "victim@gmail.com"}])
    make(odoo, customer_contact="%@gmail.com")

    assert "partner_id" not in odoo.lead()
    assert odoo.made("res.partner") == []


def test_reusing_a_partner_under_a_different_name_is_flagged_for_the_human():
    odoo = FakeOdoo(partners=[{"id": 7, "name": "Someone Else", "email": "sarah.connor@example.com"}])
    make(odoo)

    lead = odoo.lead()
    assert lead["partner_id"] == 7
    assert "differs from the contact on file" in lead["description"]
    assert "Someone Else" in lead["description"] and "Sarah Connor" in lead["description"]


def test_phone_only_customers_are_matched_by_phone():
    odoo = FakeOdoo(partners=[{"id": 9, "name": "Sarah Connor", "phone": "+971501234567"}])
    make(odoo, customer_contact="+971 50 123 4567")

    assert odoo.made("res.partner") == []
    assert odoo.lead()["partner_id"] == 9


def test_no_contact_detail_means_no_partner_but_the_name_is_kept():
    odoo = FakeOdoo()
    make(odoo, customer_contact=None)

    assert odoo.made("res.partner") == []
    lead = odoo.lead()
    assert lead["contact_name"] == "Sarah Connor"
    assert "partner_id" not in lead


@pytest.mark.parametrize("junk", ["message me on the app", "a@b.c", "2020-01-15"])
def test_a_contact_that_is_not_an_email_or_phone_is_kept_in_the_description_only(junk):
    odoo = FakeOdoo()
    make(odoo, customer_contact=junk)

    assert odoo.made("res.partner") == []
    assert junk in odoo.lead()["description"]


def test_an_absurdly_long_name_is_capped_before_it_becomes_a_partner():
    odoo = FakeOdoo()
    make(odoo, customer_name="N" * 5000)

    assert len(odoo.made("res.partner")[0]["name"]) == NAME_MAX
    assert len(odoo.lead()["contact_name"]) == NAME_MAX


# ---- source and tag -------------------------------------------------------------

def test_the_lead_is_sourced_from_telegram_and_tagged_by_catalog():
    odoo = FakeOdoo()
    make(odoo)

    assert odoo.made("utm.source")[0] == {"name": "Telegram"}
    assert odoo.made("crm.tag")[0] == {"name": "Cars"}
    lead = odoo.lead()
    assert lead["source_id"] == odoo.id_of("utm.source")
    assert lead["tag_ids"] == [(6, 0, [odoo.id_of("crm.tag")])]


def test_existing_source_and_tag_are_reused():
    odoo = FakeOdoo(sources=[{"id": 5}], tags=[{"id": 9}])
    make(odoo)

    assert odoo.made("utm.source") == [] and odoo.made("crm.tag") == []
    lead = odoo.lead()
    assert lead["source_id"] == 5
    assert lead["tag_ids"] == [(6, 0, [9])]


def test_real_estate_leads_get_the_real_estate_tag():
    odoo = FakeOdoo()
    create_verified_lead(odoo, "real_estate", {"name": "3bd house", "customer_name": "Alex Rivera", "customer_contact": "alex@example.com"})
    assert odoo.made("crm.tag")[0] == {"name": "Real estate"}


@pytest.mark.parametrize("failing", ["res.partner", "utm.source", "crm.tag"])
def test_a_failure_creating_a_helper_record_never_loses_the_lead(failing):
    odoo = FakeOdoo(fail=[failing])
    result = make(odoo)

    assert result["lead_id"]
    lead = odoo.lead()
    assert lead["name"] == "2020 Toyota Camry"
    key = {"res.partner": "partner_id", "utm.source": "source_id", "crm.tag": "tag_ids"}[failing]
    assert key not in lead


# ---- the description -------------------------------------------------------------

def test_notes_and_price_verification_still_work():
    odoo = FakeOdoo()
    result = make(odoo, price=1, notes="Wants a test drive")

    lead = odoo.lead()
    assert "Wants a test drive" in lead["description"]
    assert "Price unverified" not in lead["description"]  # the fake catalog has every price
    assert lead["expected_revenue"] == 1
    assert "price_verified" not in result


def test_the_customer_name_is_no_longer_duplicated_into_the_description():
    odoo = FakeOdoo()
    make(odoo, notes="Interested")
    description = odoo.lead()["description"]
    assert "Customer:" not in description and "Contact:" not in description


def test_the_description_is_html_with_one_paragraph_per_line():
    odoo = FakeOdoo()
    make(odoo, notes="Interested", customer_contact="message me")
    assert odoo.lead()["description"] == "<p>Notes: Interested</p><p>Contact as given: message me</p>"


def test_customer_text_in_the_description_cannot_become_markup():
    odoo = FakeOdoo()
    make(odoo, notes="<a href='https://evil.example'>claim your prize</a> & <script>x</script>")
    description = odoo.lead()["description"]
    assert "<a href" not in description and "<script>" not in description
    assert "&lt;a href=" in description and "&amp;" in description


# ---- duplicates -----------------------------------------------------------------

def test_the_same_customer_asking_again_within_minutes_gets_the_same_lead():
    odoo = FakeOdoo(existing_leads=[{"id": 4242}])
    result = make(odoo)

    assert result == {"lead_id": 4242, "duplicate": True}
    assert odoo.made("crm.lead") == []
    assert odoo.made("res.partner") == []  # no helper records for a lead that already exists


def test_the_duplicate_check_looks_at_name_contact_and_a_recent_window():
    odoo = FakeOdoo()
    make(odoo, customer_contact="sarah.connor@example.com or 0501234567")

    (model, domain), = [s for s in odoo.searches if s[0] == "crm.lead"]
    flat = repr(domain)
    assert ("name", "=", "2020 Toyota Camry") in domain
    assert "email_from" in flat and "sarah.connor@example.com" in flat
    assert "phone" in flat and "0501234567" in flat
    assert any(isinstance(c, tuple) and c[0] == "create_date" and c[1] == ">=" for c in domain)


def test_a_lead_with_no_contact_cannot_be_deduplicated_and_is_always_created():
    odoo = FakeOdoo(existing_leads=[{"id": 4242}])
    make(odoo, customer_contact=None)

    assert [s for s in odoo.searches if s[0] == "crm.lead"] == []
    assert len(odoo.made("crm.lead")) == 1


def test_a_failing_duplicate_check_never_blocks_a_new_lead():
    class Broken(FakeOdoo):
        def search_read(self, model, domain, fields):
            if model == "crm.lead":
                raise RuntimeError("odoo hiccup")
            return super().search_read(model, domain, fields)

    odoo = Broken()
    assert make(odoo)["lead_id"] == odoo.id_of("crm.lead")


# ---- filling in what the model left out ------------------------------------------

from engine.core.crm_contacts import merge_contact


@pytest.mark.parametrize(
    "model_contact, customer_text, expected",
    [
        # the model kept only the email; the customer also gave a phone
        ("a@x.com", "I'm Al, a@x.com, call me on +971 50 999 8888", "a@x.com +971509998888"),
        # the model kept only the phone; the customer also gave an email
        ("0501234567", "reach me at al@x.com or 0501234567", "0501234567 al@x.com"),
        # the model already has both: nothing added
        ("a@x.com +971509998888", "a@x.com, +971 50 999 8888", "a@x.com +971509998888"),
        # the model gave nothing at all: take what the customer typed
        (None, "my email is al@x.com", "al@x.com"),
        # a different email in the customer's text never replaces or joins the model's
        ("a@x.com", "a@x.com, and my boss is boss@x.com", "a@x.com"),
        # nothing in the customer's text: unchanged
        ("a@x.com", "please call me", "a@x.com"),
        (None, "please call me", None),
        # look-alikes are not contacts
        ("a@x.com", "the deal closed on 2020-01-15", "a@x.com"),
    ],
)
def test_merge_contact_fills_only_what_is_missing_from_the_customers_own_words(model_contact, customer_text, expected):
    assert merge_contact(model_contact, customer_text) == expected
