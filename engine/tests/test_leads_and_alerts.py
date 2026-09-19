import json

import httpx
import pytest
import respx

from engine.core.agent_loop import AgentTurnResult
from engine.leads import LeadInfo, extract_leads
from engine.llm_client import ToolCall
from engine import notifier


def _turn(calls_and_results):
    return AgentTurnResult(
        reply="ok",
        tool_calls_made=[ToolCall(name=n, arguments=a) for n, a, _ in calls_and_results],
        tool_results=[r for _, _, r in calls_and_results],
    )


LEAD_ARGS = {
    "name": "2020 Toyota Camry",
    "customer_name": "Sarah Connor",
    "customer_contact": "sarah.connor@example.com or +971 50 111 2222",
    "price": 21834,
}


# ---- extract_leads ---------------------------------------------------------------

def test_a_created_lead_is_extracted_with_its_contact_parsed():
    leads = extract_leads(_turn([("create_lead", LEAD_ARGS, {"lead_id": 71})]), domain_type="cars")

    assert leads == [
        LeadInfo(
            lead_id=71,
            domain_type="cars",
            item_name="2020 Toyota Camry",
            customer_name="Sarah Connor",
            email="sarah.connor@example.com",
            phone="+971501112222",
            price=21834,
            price_verified=True,
        )
    ]


def test_an_unverified_price_is_carried_through():
    result = {"lead_id": 8, "price_verified": False}
    (lead,) = extract_leads(_turn([("create_lead", LEAD_ARGS, result)]), domain_type="cars")
    assert lead.price_verified is False


@pytest.mark.parametrize(
    "call",
    [
        ("search_inventory", {"make": "Toyota"}, {"matches": [], "count": 0}),
        ("create_lead", LEAD_ARGS, {"error": "lead limit reached"}),
        ("create_lead", LEAD_ARGS, {"error": "invalid arguments: x"}),
    ],
)
def test_searches_and_failed_leads_are_not_leads(call):
    assert extract_leads(_turn([call]), domain_type="cars") == []


def test_two_different_leads_in_one_turn_are_both_extracted():
    turn = _turn([("create_lead", LEAD_ARGS, {"lead_id": 1}), ("create_lead", {**LEAD_ARGS, "name": "RAV4"}, {"lead_id": 2})])
    assert [l.lead_id for l in extract_leads(turn, domain_type="cars")] == [1, 2]


# ---- notifier -------------------------------------------------------------------

LEAD = LeadInfo(
    lead_id=71, domain_type="cars", item_name="2020 Toyota Camry", customer_name="Sarah Connor",
    email="sarah.connor@example.com", phone="+971501112222", price=21834, price_verified=True,
)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERTS_BOT_TOKEN", "alerts-token")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "4242")
    monkeypatch.setenv("ODOO_PUBLIC_URL", "https://odoo.example.test")


def test_nothing_is_sent_when_the_alert_bot_is_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERTS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERTS_CHAT_ID", raising=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=r"https://api\.telegram\.org/.*")
        assert notifier.send_lead_alert(LEAD) is False
        assert not route.called


@respx.mock
def test_the_alert_goes_to_the_alert_bot_and_chat(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    assert notifier.send_lead_alert(LEAD) is True

    payload = json.loads(route.calls.last.request.content)
    assert payload["chat_id"] == "4242"
    assert payload["parse_mode"] == "HTML"
    text = payload["text"]
    for expected in ("2020 Toyota Camry", "Sarah Connor", "sarah.connor@example.com", "+971501112222", "$21,834"):
        assert expected in text
    assert "https://odoo.example.test/odoo/" in text and "71" in text


@respx.mock
def test_customer_supplied_text_cannot_inject_markup_into_the_alert(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    hostile = LeadInfo(**{**LEAD.__dict__, "customer_name": "<b>Boss</b> <a href='https://evil.example'>click</a>", "item_name": "A & B <i>car</i>"})

    notifier.send_lead_alert(hostile)

    text = json.loads(route.calls.last.request.content)["text"]
    assert "<a href='https://evil.example'>" not in text
    assert "&lt;b&gt;Boss&lt;/b&gt;" in text
    assert "A &amp; B &lt;i&gt;car&lt;/i&gt;" in text


@respx.mock
def test_an_unverified_price_is_flagged_in_the_alert(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    notifier.send_lead_alert(LeadInfo(**{**LEAD.__dict__, "price_verified": False, "price": 1}))
    text = json.loads(route.calls.last.request.content)["text"]
    assert "not verified" in text.lower()


@respx.mock
def test_a_telegram_error_is_reported_as_false_and_never_raised(configured):
    respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(return_value=httpx.Response(401, json={"ok": False}))
    assert notifier.send_lead_alert(LEAD) is False


@respx.mock
def test_a_network_failure_is_reported_as_false_and_never_raised(configured):
    respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(side_effect=httpx.ConnectTimeout("slow"))
    assert notifier.send_lead_alert(LEAD) is False


@respx.mock
def test_a_lead_without_contact_or_price_still_produces_an_alert(configured):
    route = respx.post("https://api.telegram.org/botalerts-token/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    bare = LeadInfo(lead_id=3, domain_type="real_estate", item_name="3bd house", customer_name="Alex", email=None, phone=None, price=None, price_verified=True)
    assert notifier.send_lead_alert(bare) is True
    text = json.loads(route.calls.last.request.content)["text"]
    assert "Alex" in text and "no contact" in text.lower()


def test_a_duplicate_result_is_not_a_new_lead():
    """A retry that gets the earlier lead back must not alert or mirror it again."""
    turn = _turn([("create_lead", LEAD_ARGS, {"lead_id": 71, "duplicate": True})])
    assert extract_leads(turn, domain_type="cars") == []
