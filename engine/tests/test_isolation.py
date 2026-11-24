"""The test run must never see real service credentials, whatever is in a developer's .env."""
import os

import pytest

REAL_SERVICE_VARS = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "CHAT_REF_SECRET",
    "TELEGRAM_ALERTS_BOT_TOKEN",
    "TELEGRAM_ALERTS_CHAT_ID",
    "MISTRAL_API_KEY",
    "NVIDIA_API_KEY",
]


@pytest.mark.parametrize("name", REAL_SERVICE_VARS)
def test_real_service_credentials_are_blank_during_tests(name):
    assert os.environ.get(name, "") == ""


def test_required_settings_are_present_without_a_dotenv_file():
    from engine.config import load_config

    config = load_config()
    assert config["ODOO_URL"].startswith("http://")
    assert config["ACTIVE_DOMAIN"] == "cars"
    assert os.environ["OPENROUTER_API_KEY"] == "test-key-not-real"


# Optional settings are listed in .env.example as commented lines. A developer's real .env can set any
# of them, and none of them may reach the test run.
OPTIONAL_VARS = [
    "TRACE_FILE",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_SEED",
    "LLM_PRICE_PER_MTOK_INPUT",
    "LLM_PRICE_PER_MTOK_OUTPUT",
    "LOG_LEVEL",
    "ODOO_WEBHOOK_SECRET",
    "ODOO_STATUS_WEBHOOK_URL",
    "ODOO_TIMEOUT_SECONDS",
    "OPENROUTER_MODELS",
    "EVAL_RECORD_LEADS",
]


@pytest.mark.parametrize("name", OPTIONAL_VARS)
def test_optional_settings_are_blank_during_tests(name):
    assert os.environ.get(name, "") == ""


def test_the_names_checked_above_are_the_ones_in_env_example():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    assert set(OPTIONAL_VARS) <= listed
