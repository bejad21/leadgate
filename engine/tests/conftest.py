"""Keep every test away from live services.

Posting to the webhook writes to Supabase and the store and can alert a real Telegram bot, so without these stubs a test
would put fake conversations into the live dashboard tables or message the owner. Tests that check those calls patch them
again on top of these stubs.
"""
from unittest.mock import MagicMock

import pytest

import engine.main as main_module
from engine import store as store_module
from engine.notifier import Delivery
from engine.tests.fakes import make_world


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    monkeypatch.setattr(main_module, "supabase_sync", MagicMock())
    monkeypatch.setattr(main_module, "deliver_lead_alert", MagicMock(return_value=Delivery(False)))
    previous = store_module._store
    store_module.set_store(store_module.InMemoryStore())
    yield
    store_module.set_store(previous)


@pytest.fixture
def world():
    return make_world()
