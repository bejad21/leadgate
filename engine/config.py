import os

from dotenv import load_dotenv

from engine.llm_client import FailoverLLM, LLMClient

# base_url and model id per LLM provider. OpenRouter's free tier needs a ":free" model id; Mistral is
# OpenAI-compatible but has its own base_url.
_PROVIDER_SETTINGS = {
    "MISTRAL_API_KEY": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
    },
    "OPENROUTER_API_KEY": {
        "base_url": "https://openrouter.ai/api/v1",
        # OpenRouter's free catalog rotates, so models are tried in order (see FailoverLLM) and
        # OPENROUTER_MODELS in .env overrides them.
        "models": ["nvidia/nemotron-3-ultra-550b-a55b:free", "nex-agi/nex-n2.5-pro:free"],
    },
}

# Preference order when several LLM keys are present: OpenRouter first, Mistral as the fallback.
_PROVIDER_PREFERENCE = ["OPENROUTER_API_KEY", "MISTRAL_API_KEY"]

REQUIRED_VARS = [
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_USER",
    "ODOO_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ACTIVE_DOMAIN",
]

# At least one of these must be present.
LLM_KEY_VARS = ["OPENROUTER_API_KEY", "MISTRAL_API_KEY"]


def load_config() -> dict:
    """Load required environment variables from .env / the environment.

    Raises RuntimeError listing every missing variable, including the case where no LLM API key is set.
    """
    load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]

    llm_keys_present = {name: os.environ.get(name) for name in LLM_KEY_VARS if os.environ.get(name)}
    if not llm_keys_present:
        missing.append(f"one of ({', '.join(LLM_KEY_VARS)})")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    config = {name: os.environ[name] for name in REQUIRED_VARS}
    config.update(llm_keys_present)
    return config


def _openrouter_models() -> list[str]:
    configured = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", "").split(",") if m.strip()]
    return configured or list(_PROVIDER_SETTINGS["OPENROUTER_API_KEY"]["models"])


def get_llm_client(config: dict):
    """Build the language-model client from whichever provider keys are configured.

    Each configured provider contributes its models in preference order (OpenRouter's list, then
    Mistral). They are wrapped in a FailoverLLM, so a retired or rate-limited model hands over to
    the next instead of failing every customer message. With a single model the plain client is
    returned.
    """
    clients = []
    for key_name in _PROVIDER_PREFERENCE:
        if not config.get(key_name):
            continue
        settings = _PROVIDER_SETTINGS[key_name]
        models = _openrouter_models() if key_name == "OPENROUTER_API_KEY" else [settings["model"]]
        for model in models:
            clients.append(LLMClient(api_key=config[key_name], model=model, base_url=settings["base_url"]))
    if not clients:
        raise RuntimeError("No LLM API key configured: expected one of " + ", ".join(_PROVIDER_PREFERENCE))
    return clients[0] if len(clients) == 1 else FailoverLLM(clients)
