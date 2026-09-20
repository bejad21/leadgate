import os

from dotenv import load_dotenv

from engine.llm_client import FailoverLLM, LLMClient

# Provider-specific settings: base_url and a concrete model id for whichever
# LLM API key is actually configured. OpenRouter requires a ":free"-suffixed
# model on the free tier; Mistral's API is OpenAI-compatible for chat/tools
# but needs its own base_url and model id.
_PROVIDER_SETTINGS = {
    "MISTRAL_API_KEY": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
    },
    "OPENROUTER_API_KEY": {
        "base_url": "https://openrouter.ai/api/v1",
        # OpenRouter's free-tier catalog rotates, so this is a list tried in order (see
        # FailoverLLM), and OPENROUTER_MODELS in .env replaces it. Two earlier choices have
        # already been retired: "meta-llama/llama-3.3-70b-instruct:free" went paid-only and
        # "deepseek/deepseek-v4-flash-0731:free" left the catalog. The ones below were checked
        # on 2026-09-20 against openrouter.ai/api/v1/models and return correct tool_calls for
        # our exact function-calling schema.
        "models": ["nvidia/nemotron-3-ultra-550b-a55b:free", "nex-agi/nex-n2.5-pro:free"],
    },
}

# Preference order when more than one LLM key happens to be present.
# OpenRouter is tried first: the configured Mistral account has been
# confirmed (live, 2026-09-18) to return HTTP 429 with a 0 req/minute quota
# on chat completions across two different API keys, while listing models
# succeeds -- an account-level restriction, not a transient rate limit.
# OpenRouter remains second so a working Mistral account still gets used
# automatically if this ever changes.
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

    Fails fast with a clear RuntimeError listing every missing variable if
    any required variable (or none of the acceptable LLM API keys) is set.
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
