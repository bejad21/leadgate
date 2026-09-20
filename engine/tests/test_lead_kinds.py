"""Reservations and viewings are leads too: what is extracted from a turn."""
from engine.core.agent_loop import AgentTurnResult
from engine.leads import extract_leads
from engine.llm_client import ToolCall

LEAD_ARGS = {
    "name": "2020 Toyota Camry",
    "customer_name": "Sarah Connor",
    "customer_contact": "sarah.connor@example.com or +971 50 111 2222",
    "price": 21834,
}


def _turn(calls_and_results):
    return AgentTurnResult(
        reply="ok",
        tool_calls_made=[ToolCall(name=n, arguments=a) for n, a, _ in calls_and_results],
        tool_results=[r for _, _, r in calls_and_results],
    )


def test_a_reservation_is_extracted_as_a_reservation_with_the_catalogs_facts():
    call = ("reserve_item", {"item_id": 7, "customer_name": "Sarah Connor", "customer_contact": "sarah@example.com"},
            {"lead_id": 5, "kind": "reservation", "item_name": "2024 Toyota Corolla", "price": 21950.0, "detail": "Held for 24 hours"})
    [lead] = extract_leads(_turn([call]), domain_type="cars")
    assert (lead.kind, lead.item_name, lead.price, lead.detail) == ("reservation", "2024 Toyota Corolla", 21950.0, "Held for 24 hours")
    assert lead.email == "sarah@example.com" and lead.price_verified is True


def test_a_viewing_is_extracted_with_its_day_and_slot():
    call = ("book_viewing", {"item_id": 7, "customer_name": "Sarah", "customer_contact": "+971501112222", "date": "2026-09-26", "slot": "afternoon"},
            {"lead_id": 6, "kind": "viewing", "item_name": "2024 Toyota Corolla", "price": 21950.0, "detail": "Sat 26 Sep, afternoon"})
    [lead] = extract_leads(_turn([call]), domain_type="cars")
    assert (lead.kind, lead.detail, lead.phone) == ("viewing", "Sat 26 Sep, afternoon", "+971501112222")


def test_an_ordinary_lead_stays_a_lead():
    [lead] = extract_leads(_turn([("create_lead", LEAD_ARGS, {"lead_id": 1})]), domain_type="cars")
    assert lead.kind == "lead" and lead.detail is None


def test_a_refused_reservation_is_not_a_lead():
    call = ("reserve_item", {"item_id": 7, "customer_name": "x"}, {"error": "That item is no longer available."})
    assert extract_leads(_turn([call]), domain_type="cars") == []


def test_a_repeated_reservation_is_not_alerted_twice():
    call = ("reserve_item", {"item_id": 7, "customer_name": "x"}, {"lead_id": 5, "kind": "reservation", "item_name": "X", "duplicate": True})
    assert extract_leads(_turn([call]), domain_type="cars") == []


def test_an_unknown_tool_that_returns_a_lead_id_is_ignored():
    call = ("mystery", {}, {"lead_id": 9, "kind": "lead", "item_name": "X"})
    assert extract_leads(_turn([call]), domain_type="cars") == []
