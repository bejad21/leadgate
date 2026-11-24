"""The Odoo addon's side of the status webhook: what it sends and when it stays silent.

The addon needs Odoo to import, so a minimal stand-in for the `odoo` package is installed just for the
import. What is under test is the addon's own code: the payload, the secret header and the no-op cases.
"""
import datetime as dt
import importlib.util
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[2] / "odoo" / "addons" / "leadgate_domain" / "models" / "lead_webhook.py"


@pytest.fixture
def addon(monkeypatch):
    odoo = types.ModuleType("odoo")
    odoo.models = types.SimpleNamespace(Model=object)  # type: ignore[attr-defined]
    odoo.fields = types.SimpleNamespace(Datetime=types.SimpleNamespace(to_string=lambda value: value.strftime("%Y-%m-%d %H:%M:%S")))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    spec = importlib.util.spec_from_file_location("lead_webhook_under_test", ADDON)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Params:
    def __init__(self, values):
        self.values = values

    def sudo(self):
        return self

    def get_param(self, key):
        return self.values.get(key)


class Record:
    def __init__(self, **fields):
        self.__dict__.update(id=7, domain_type="cars", status="sold", name="Camry", price=21834.0, **fields)


def item(addon, params, records):
    model = addon.CatalogItem.__new__(addon.CatalogItem)
    model.env = {"ir.config_parameter": Params(params)}
    model.__class__ = type("Records", (addon.CatalogItem,), {"__iter__": lambda self: iter(records)})
    return model


@pytest.fixture
def sent(monkeypatch, addon):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))

    monkeypatch.setattr(addon.urllib.request, "urlopen", fake_urlopen)
    return requests


WRITTEN = dt.datetime(2026, 9, 20, 10, 0, 0)


def test_it_posts_the_event_with_the_secret_in_a_header(addon, sent):
    params = {addon.WEBHOOK_URL_PARAM: "http://engine:8000/webhook/odoo-status", addon.WEBHOOK_SECRET_PARAM: "s3cret-s3cret-s3cret"}
    item(addon, params, [Record(write_date=WRITTEN)])._notify_webhook()
    [(request, timeout)] = sent
    assert request.full_url == "http://engine:8000/webhook/odoo-status" and request.get_method() == "POST"
    assert request.get_header("X-odoo-webhook-secret") == "s3cret-s3cret-s3cret"
    assert json.loads(request.data) == {"id": 7, "domain_type": "cars", "status": "sold", "name": "Camry", "price": 21834.0, "write_date": "2026-09-20 10:00:00"}
    assert timeout == addon.WEBHOOK_TIMEOUT_SECONDS


def test_the_secret_is_never_part_of_the_url_or_the_body(addon, sent):
    params = {addon.WEBHOOK_URL_PARAM: "http://engine:8000/webhook/odoo-status", addon.WEBHOOK_SECRET_PARAM: "s3cret-s3cret-s3cret"}
    item(addon, params, [Record(write_date=WRITTEN)])._notify_webhook()
    [(request, _)] = sent
    assert "s3cret" not in request.full_url and b"s3cret" not in request.data


@pytest.mark.parametrize("missing", ["url", "secret"])
def test_without_the_url_or_the_secret_it_sends_nothing(addon, sent, missing):
    params = {addon.WEBHOOK_URL_PARAM: "http://engine:8000/x", addon.WEBHOOK_SECRET_PARAM: "s3cret-s3cret-s3cret"}
    params[addon.WEBHOOK_URL_PARAM if missing == "url" else addon.WEBHOOK_SECRET_PARAM] = ""
    item(addon, params, [Record(write_date=WRITTEN)])._notify_webhook()
    assert sent == []


def test_it_sends_one_event_per_record(addon, sent):
    params = {addon.WEBHOOK_URL_PARAM: "http://engine:8000/x", addon.WEBHOOK_SECRET_PARAM: "s3cret-s3cret-s3cret"}
    item(addon, params, [Record(write_date=WRITTEN), Record(write_date=WRITTEN)])._notify_webhook()
    assert len(sent) == 2


def test_a_failed_call_is_logged_without_the_secret_and_never_raised(addon, monkeypatch, caplog):
    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(addon.urllib.request, "urlopen", refuse)
    params = {addon.WEBHOOK_URL_PARAM: "http://engine:8000/x", addon.WEBHOOK_SECRET_PARAM: "s3cret-s3cret-s3cret"}
    item(addon, params, [Record(write_date=WRITTEN)])._notify_webhook()
    assert "failed to notify" in caplog.text and "s3cret" not in caplog.text


def test_the_old_sync_parameter_is_not_read(addon):
    assert "sync_webhook" not in addon.WEBHOOK_URL_PARAM and "sync_webhook" not in ADDON.read_text(encoding="utf-8")
