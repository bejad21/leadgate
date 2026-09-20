import pytest

import engine.config as config_module
from engine.llm_client import FailoverLLM
from engine.config import get_llm_client, load_config


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
    "ACTIVE_DOMAIN": "cars",
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


def test_get_llm_client_uses_mistral_settings_when_only_mistral_key_present(monkeypatch):
    for key, value in BASE_ENV.items():
        if key == "OPENROUTER_API_KEY":
            continue
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")

    client = get_llm_client(load_config())

    assert client.api_key == "test-mistral-key"
    assert client.base_url == "https://api.mistral.ai/v1"
    assert client.model == "mistral-small-latest"


def test_openrouter_alone_gives_an_ordered_failover_of_its_models(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)

    client = get_llm_client(load_config())

    assert isinstance(client, FailoverLLM)
    assert len(client.clients) >= 2
    assert all(c.base_url == "https://openrouter.ai/api/v1" and c.api_key == "test-openrouter-key" for c in client.clients)
    assert all(c.model.endswith(":free") for c in client.clients)
    assert "deepseek/deepseek-v4-flash-0731:free" not in [c.model for c in client.clients]  # retired


def test_the_model_list_can_be_overridden_from_the_environment(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODELS", " vendor/a:free , vendor/b:free ,, ")

    client = get_llm_client(load_config())

    assert [c.model for c in client.clients] == ["vendor/a:free", "vendor/b:free"]


def test_a_single_configured_model_is_returned_without_a_wrapper(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODELS", "vendor/only:free")

    client = get_llm_client(load_config())

    assert not isinstance(client, FailoverLLM) and client.model == "vendor/only:free"


def test_both_providers_configured_puts_mistral_after_the_openrouter_models(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)

    client = get_llm_client(load_config())

    assert client.clients[-1].base_url == "https://api.mistral.ai/v1"
    assert client.clients[0].base_url == "https://openrouter.ai/api/v1"
