"""Keep every test off live services, whatever is in a developer's .env.

This loads before any test imports `engine.main`, which reads settings at import time. Every name in `.env.example`,
including the optional ones commented out there, is forced to a harmless value, so the suite passes on a clean clone
and can never reach live Odoo, Supabase, the model API or the Telegram bots.
"""
import os
import re
from pathlib import Path

SAFE_VALUES = {
    "ODOO_URL": "http://odoo.test",
    "ODOO_DB": "test_db",
    "ODOO_USER": "test_user",
    "ODOO_PASSWORD": "test-password-not-real",
    "TELEGRAM_BOT_TOKEN": "123456:test-token-not-real",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "ACTIVE_DOMAIN": "cars",
    "OPENROUTER_API_KEY": "test-key-not-real",
    # Optional settings whose blank value would mean something other than "use the default".
    "DEFAULT_PHONE_COUNTRY_CODE": "971",
}


def _names_in_env_example() -> list[str]:
    text = (Path(__file__).parent / ".env.example").read_text(encoding="utf-8")
    return re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)


for _name in _names_in_env_example():
    os.environ[_name] = SAFE_VALUES.get(_name, "")
for _name, _value in SAFE_VALUES.items():
    os.environ[_name] = _value
