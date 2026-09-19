"""Keep every test away from the real outside world.

The webhook writes to Supabase and can alert a real Telegram bot. Without this,
any test that posts to the webhook would put fake conversations into the live
dashboard tables and could message the owner. Tests that check those calls patch
them again, on top of these stubs.
"""
from unittest.mock import MagicMock

import pytest

import engine.main as main_module


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    monkeypatch.setattr(main_module, "supabase_sync", MagicMock())
    monkeypatch.setattr(main_module, "send_lead_alert", MagicMock(return_value=False))
