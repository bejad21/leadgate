"""The dashboard's copy of a lead: what kind it is, and where it stands."""
import json

import httpx
import pytest
import respx

from engine import supabase_sync
from engine.leads import LeadInfo

BASE = "https://example.supabase.test"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setenv("CHAT_REF_SECRET", "test-secret")


def lead(**extra):
    base = dict(lead_id=71, domain_type="cars", item_name="2024 Toyota Corolla", customer_name="Sarah", email=None, phone=None, price=21950.0, price_verified=True)
    return LeadInfo(**{**base, **extra})


@respx.mock
def test_the_kind_and_detail_are_stored():
    route = respx.post(f"{BASE}/rest/v1/leads").mock(return_value=httpx.Response(201))
    supabase_sync.record_lead(555, lead(kind="viewing", detail="Sat 26 Sep, afternoon"))
    body = json.loads(route.calls.last.request.content)
    assert body["kind"] == "viewing" and body["detail"] == "Sat 26 Sep, afternoon"


@respx.mock
def test_recording_a_lead_never_resets_its_status():
    """The upsert merges, so a status column in the body would put a handled lead back to new."""
    route = respx.post(f"{BASE}/rest/v1/leads").mock(return_value=httpx.Response(201))
    supabase_sync.record_lead(555, lead())
    assert "status" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_a_status_change_patches_only_that_lead():
    route = respx.patch(f"{BASE}/rest/v1/leads").mock(return_value=httpx.Response(204))
    assert supabase_sync.update_lead(71, {"status": "contacted"}) is True
    request = route.calls.last.request
    assert request.url.params["odoo_lead_id"] == "eq.71"
    assert json.loads(request.content) == {"status": "contacted"}
    assert request.headers["authorization"] == "Bearer service-key"


@respx.mock
@pytest.mark.parametrize("effect", [httpx.Response(500), httpx.Response(401), httpx.ConnectTimeout("slow")])
def test_a_failed_status_update_is_false_not_an_exception(effect):
    respx.patch(f"{BASE}/rest/v1/leads").mock(side_effect=[effect] if isinstance(effect, Exception) else None, return_value=None if isinstance(effect, Exception) else effect)
    assert supabase_sync.update_lead(71, {"status": "won"}) is False


def test_without_supabase_configured_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.patch(url__regex=r".*")
        assert supabase_sync.update_lead(71, {"status": "won"}) is False
        assert not route.called


def test_only_known_statuses_are_accepted():
    with respx.mock(assert_all_called=False) as router:
        route = router.patch(url__regex=r".*")
        assert supabase_sync.update_lead(71, {"status": "<script>"}) is False
        assert not route.called
