import re
from pathlib import Path

import pytest

import engine.config as config_module
from engine.config import configured_llm_models, get_llm_client, load_config
from engine.llm_client import FailoverLLM, LLMClient


@pytest.fixture(autouse=True)
def isolate_from_real_dotenv_file(monkeypatch):
    """Prevent the repo's real .env file from leaking values into these tests."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)


BASE_ENV = {
    "ODOO_URL": "http://localhost:8069",
    "ODOO_DB": "leadgate",
    "ODOO_USER": "admin",
    "ODOO_PASSWORD": "a-test-password",
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
    assert config["ODOO_PASSWORD"] == "a-test-password"
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


@pytest.mark.parametrize("domain", ["cars", "real_estate"])
def test_each_known_domain_is_accepted(monkeypatch, domain):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ACTIVE_DOMAIN", domain)

    assert load_config()["ACTIVE_DOMAIN"] == domain


@pytest.mark.parametrize("domain", ["car", "realestate", "Cars", "boats"])
def test_an_unknown_domain_fails_at_startup_naming_the_allowed_values(monkeypatch, domain):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ACTIVE_DOMAIN", domain)

    with pytest.raises(RuntimeError, match=r"ACTIVE_DOMAIN.*cars, real_estate"):
        load_config()


def test_the_engine_and_the_eval_runner_share_one_adapter_registry():
    from engine.adapters import ADAPTERS
    from engine.adapters.cars import CarsAdapter
    from engine.adapters.real_estate import RealEstateAdapter
    from eval import run_eval

    assert ADAPTERS == {"cars": CarsAdapter, "real_estate": RealEstateAdapter}
    assert run_eval.ADAPTERS is ADAPTERS


def test_get_llm_client_uses_mistral_settings_when_only_mistral_key_present(monkeypatch):
    for key, value in BASE_ENV.items():
        if key == "OPENROUTER_API_KEY":
            continue
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")

    client = get_llm_client(load_config())

    assert isinstance(client, LLMClient)
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
    assert "deepseek/deepseek-v4-flash-0731:free" not in [c.model for c in client.clients]  # model id removed from OpenRouter


def test_the_model_list_can_be_overridden_from_the_environment(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODELS", " vendor/a:free , vendor/b:free ,, ")

    client = get_llm_client(load_config())

    assert isinstance(client, FailoverLLM)
    assert [c.model for c in client.clients] == ["vendor/a:free", "vendor/b:free"]


def test_a_single_configured_model_is_returned_without_a_wrapper(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODELS", "vendor/only:free")

    client = get_llm_client(load_config())

    assert isinstance(client, LLMClient) and client.model == "vendor/only:free"


def test_both_providers_configured_puts_mistral_after_the_openrouter_models(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)

    client = get_llm_client(load_config())

    assert isinstance(client, FailoverLLM)
    assert client.clients[-1].base_url == "https://api.mistral.ai/v1"
    assert client.clients[0].base_url == "https://openrouter.ai/api/v1"


def test_every_model_in_the_failover_shares_the_sampling_settings_read_at_build_time(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LLM_TEMPERATURE", "0.4")
    monkeypatch.setenv("LLM_SEED", "7")

    client = get_llm_client(load_config())

    assert isinstance(client, FailoverLLM)
    assert {(c.sampling.temperature, c.sampling.seed) for c in client.clients} == {(0.4, 7)}


def test_a_bad_sampling_value_stops_the_client_build_and_names_the_variable(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LLM_MAX_TOKENS", "0")

    with pytest.raises(ValueError, match="LLM_MAX_TOKENS"):
        get_llm_client(load_config())


def _mistral_only(monkeypatch):
    for key, value in BASE_ENV.items():
        if key != "OPENROUTER_API_KEY":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")


def test_mistral_model_pins_a_dated_id(monkeypatch):
    _mistral_only(monkeypatch)
    monkeypatch.setenv("MISTRAL_MODEL", " mistral-small-2603 ")
    assert get_llm_client(load_config()).model == "mistral-small-2603"  # type: ignore[attr-defined]


def test_a_blank_mistral_model_keeps_the_default(monkeypatch):
    _mistral_only(monkeypatch)
    monkeypatch.setenv("MISTRAL_MODEL", "  ")
    assert get_llm_client(load_config()).model == "mistral-small-latest"  # type: ignore[attr-defined]


@pytest.mark.parametrize("bad", ["mistral small", "mis;tral", "-x", "a" * 200])
def test_a_malformed_mistral_model_is_refused(monkeypatch, bad):
    _mistral_only(monkeypatch)
    monkeypatch.setenv("MISTRAL_MODEL", bad)
    with pytest.raises(ValueError, match="MISTRAL_MODEL"):
        get_llm_client(load_config())


def _nvidia_only(monkeypatch):
    for key, value in BASE_ENV.items():
        if key != "OPENROUTER_API_KEY":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")


def test_one_nvidia_key_is_enough_and_uses_the_nvidia_address_and_large_model(monkeypatch):
    _nvidia_only(monkeypatch)
    client = get_llm_client(load_config())
    assert isinstance(client, LLMClient)
    assert client.api_key == "test-nvidia-key"
    assert client.base_url == "https://integrate.api.nvidia.com/v1"
    assert client.model == "mistralai/mistral-large-4-1t-a52b"


def test_nvidia_model_can_be_pinned_and_a_malformed_one_is_refused(monkeypatch):
    _nvidia_only(monkeypatch)
    monkeypatch.setenv("NVIDIA_MODEL", " meta/llama-3.3-70b-instruct ")
    assert get_llm_client(load_config()).model == "meta/llama-3.3-70b-instruct"  # type: ignore[attr-defined]
    monkeypatch.setenv("NVIDIA_MODEL", "not a model")
    with pytest.raises(ValueError, match="NVIDIA_MODEL"):
        get_llm_client(load_config())


def test_nvidia_is_tried_first_then_openrouter_then_mistral(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("OPENROUTER_MODELS", "vendor/a:free,vendor/b:free")
    names = [name for name, _ in configured_llm_models(load_config())]
    assert names == ["NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"]


def test_env_example_ships_the_provider_keys_blank():
    # A made-up placeholder counts as a configured key. The provider answers 401, which does not fail over,
    # so an unused line left as copied would stop every reply.
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    for name in config_module.LLM_KEY_VARS:
        assert re.search(rf"^{name}=[ \t]*$", text, flags=re.MULTILINE), name


def test_a_short_telegram_webhook_secret_stops_the_engine_at_startup(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "short")
    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET must be at least 16"):
        load_config()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "x" * 16)
    assert load_config()["TELEGRAM_WEBHOOK_SECRET"] == "x" * 16


def test_the_alert_bot_route_refuses_everything_while_its_secret_is_short(monkeypatch):
    from fastapi.testclient import TestClient

    from engine.main import app

    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "short")
    response = TestClient(app).post("/webhook/alerts", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "short"})
    assert response.status_code == 503
    monkeypatch.setenv("TELEGRAM_ALERTS_WEBHOOK_SECRET", "s" * 20)
    assert TestClient(app).post("/webhook/alerts", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 401
