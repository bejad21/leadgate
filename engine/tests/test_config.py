import pytest

import engine.config as config_module
from engine.config import load_config


@pytest.fixture(autouse=True)
def isolate_from_real_dotenv_file(monkeypatch):
    """Prevent the repo's real .env file from leaking values into these tests."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)


BASE_ENV = {
    "ODOO_URL": "http://localhost:8069",
    "ODOO_DB": "leadgate",
    "ODOO_USER": "admin",
    "ODOO_PASSWORD": "admin",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "TELEGRAM_BOT_TOKEN": "test-telegram-token",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
}


def test_load_config_succeeds_with_all_required_vars(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    config = load_config()

    assert config["ODOO_URL"] == "http://localhost:8069"
    assert config["ODOO_DB"] == "leadgate"
    assert config["ODOO_USER"] == "admin"
    assert config["ODOO_PASSWORD"] == "admin"
    assert config["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert config["TELEGRAM_BOT_TOKEN"] == "test-telegram-token"


def test_load_config_succeeds_with_mistral_key_instead_of_openrouter(monkeypatch):
    for key, value in BASE_ENV.items():
        if key == "OPENROUTER_API_KEY":
            continue
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")

    config = load_config()

    assert config["MISTRAL_API_KEY"] == "test-mistral-key"


def test_load_config_fails_fast_on_missing_required_var(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ODOO_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="ODOO_PASSWORD"):
        load_config()


def test_load_config_fails_fast_when_no_llm_key_present(monkeypatch):
    for key, value in BASE_ENV.items():
        if key == "OPENROUTER_API_KEY":
            continue
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY.*MISTRAL_API_KEY|MISTRAL_API_KEY.*OPENROUTER_API_KEY"):
        load_config()
