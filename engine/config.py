import os

from dotenv import load_dotenv

from engine.llm_client import LLMClient

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
        # OpenRouter's free-tier catalog rotates; "meta-llama/llama-3.3-70b-
        # instruct:free" (the original choice) was moved to paid-only and now
        # 404s. Re-verified live against openrouter.ai/api/v1/models on
        # 2026-09-18: this one is free and confirmed to return correct
        # tool_calls for our exact function-calling schema.
        "model": "deepseek/deepseek-v4-flash-0731:free",
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


def get_llm_client(config: dict) -> LLMClient:
    """Build an LLMClient for whichever LLM provider key is actually
    configured, using that provider's real base_url and a model id valid
    for it (Mistral and OpenRouter are not interchangeable here).
    """
    for key_name in _PROVIDER_PREFERENCE:
        if config.get(key_name):
            settings = _PROVIDER_SETTINGS[key_name]
            return LLMClient(
                api_key=config[key_name],
                model=settings["model"],
                base_url=settings["base_url"],
            )
    raise RuntimeError(
        "No LLM API key configured: expected one of "
        + ", ".join(_PROVIDER_PREFERENCE)
    )
