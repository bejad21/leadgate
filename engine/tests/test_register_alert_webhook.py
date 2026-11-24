import json

import httpx
import pytest
import respx

from engine import register_alert_webhook as reg


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERTS_BOT_TOKEN", "alerts-token")
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "s3cret")


@respx.mock
def test_it_registers_the_alert_route_with_the_secret_and_only_the_updates_it_needs():
    route = respx.post("https://api.telegram.org/botalerts-token/setWebhook").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert reg.register("https://demo.example.test/") is True
    body = json.loads(route.calls[0].request.content)
    assert body["url"] == "https://demo.example.test/webhook/alerts"
    assert body["secret_token"] == "s3cret"
    assert sorted(body["allowed_updates"]) == ["callback_query", "message"]


@respx.mock
def test_a_rejected_registration_is_reported_false():
    respx.post("https://api.telegram.org/botalerts-token/setWebhook").mock(return_value=httpx.Response(400, json={"ok": False, "description": "bad url"}))
    assert reg.register("https://demo.example.test") is False


@pytest.mark.parametrize("url", ["", "http://insecure.example.test", "not a url", "https://"])
def test_only_a_public_https_address_is_accepted(url):
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=r".*")
        assert reg.register(url) is False
        assert not route.called


def test_it_refuses_to_run_without_a_secret(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERTS_WEBHOOK_SECRET")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=r".*")
        assert reg.register("https://demo.example.test") is False
        assert not route.called


@respx.mock
def test_the_webhook_can_be_removed():
    route = respx.post("https://api.telegram.org/botalerts-token/deleteWebhook").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert reg.remove() is True and route.called
