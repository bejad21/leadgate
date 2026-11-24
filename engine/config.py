import os
import re

from dotenv import load_dotenv

from engine.adapters import ADAPTERS
from engine.llm_client import ChatModel, FailoverLLM, LLMClient, Sampling

# base_url and model id per LLM provider. OpenRouter's free tier needs a ":free" model id; Mistral and
# NVIDIA's hosted API (build.nvidia.com, free for development) are OpenAI-compatible with their own base_url.
_PROVIDER_SETTINGS = {
    "MISTRAL_API_KEY": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
    },
    "NVIDIA_API_KEY": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "mistralai/mistral-large-4-1t-a52b",
    },
    "OPENROUTER_API_KEY": {
        "base_url": "https://openrouter.ai/api/v1",
        # OpenRouter's free catalog rotates, so models are tried in order (see FailoverLLM) and
        # OPENROUTER_MODELS in .env overrides them.
        "models": ["nvidia/nemotron-3-ultra-550b-a55b:free", "nex-agi/nex-n2.5-pro:free"],
    },
}

# Preference order when several LLM keys are present: OpenRouter first, then Mistral, then NVIDIA as fallbacks.
_PROVIDER_PREFERENCE = ["OPENROUTER_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY"]

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
LLM_KEY_VARS = list(_PROVIDER_PREFERENCE)


# A webhook secret is the only thing that tells Telegram's calls from anyone else's, so a short one is refused.
MIN_WEBHOOK_SECRET_LENGTH = 16


def webhook_secret_problem(name: str, secret: str | None) -> str | None:
    """Why `secret` cannot protect a webhook, or None."""
    if secret is not None and len(secret.strip()) < MIN_WEBHOOK_SECRET_LENGTH:
        return f"{name} must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters"
    return None


def load_config() -> dict:
    """Load required environment variables from .env / the environment.

    Raises RuntimeError listing every missing variable, including the case where no LLM API key is set,
    or naming the allowed values if ACTIVE_DOMAIN is not one of them.
    """
    load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]

    llm_keys_present = {name: os.environ[name] for name in LLM_KEY_VARS if os.environ.get(name)}
    if not llm_keys_present:
        missing.append(f"one of ({', '.join(LLM_KEY_VARS)})")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
    weak = webhook_secret_problem("TELEGRAM_WEBHOOK_SECRET", os.environ["TELEGRAM_WEBHOOK_SECRET"])
    if weak:
        raise RuntimeError(weak + " (generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    if os.environ["ACTIVE_DOMAIN"] not in ADAPTERS:
        # Caught here, at startup, rather than as a wrong catalog or a failure on the first message.
        raise RuntimeError(
            f"ACTIVE_DOMAIN={os.environ['ACTIVE_DOMAIN']!r} is not a known domain. "
            f"Use one of: {', '.join(ADAPTERS)}"
        )

    config = {name: os.environ[name] for name in REQUIRED_VARS}
    config.update(llm_keys_present)
    return config


def _openrouter_models() -> list[str]:
    configured = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", "").split(",") if m.strip()]
    return configured or list(_PROVIDER_SETTINGS["OPENROUTER_API_KEY"]["models"])


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}")


def _pinned_model(key_name: str, variable: str) -> str:
    """The model named in `variable` if set (a dated id such as mistral-small-2603 pins the model), else the
    provider's shipped default. A value with spaces or odd characters is a mistake, so it stops the start
    instead of failing on every call."""
    configured = os.environ.get(variable, "").strip()
    if not configured:
        return _PROVIDER_SETTINGS[key_name]["model"]
    if not _MODEL_ID.fullmatch(configured):
        raise ValueError(f"{variable}={configured!r} is not a model id (letters, digits and . _ : / - only)")
    return configured


_MODEL_VARIABLES = {"MISTRAL_API_KEY": "MISTRAL_MODEL", "NVIDIA_API_KEY": "NVIDIA_MODEL"}


def configured_llm_models(config: dict) -> list[tuple[str, str]]:
    """(provider key name, model id) for every model the configured keys give, in the order they are tried."""
    models = []
    for key_name in _PROVIDER_PREFERENCE:
        if not config.get(key_name):
            continue
        if key_name == "OPENROUTER_API_KEY":
            provider_models = _openrouter_models()
        else:
            provider_models = [_pinned_model(key_name, _MODEL_VARIABLES[key_name])]
        for model in provider_models:
            models.append((key_name, model))
    return models


def get_llm_client(config: dict) -> ChatModel:
    """Build the language-model client from whichever provider keys are configured.

    Each configured provider contributes its models in preference order (OpenRouter's list, then
    Mistral, then NVIDIA). They are wrapped in a FailoverLLM, so a retired or rate-limited model hands over to
    the next instead of failing every customer message. With a single model the plain client is
    returned.
    """
    sampling = Sampling.from_env()  # read once here, so a bad LLM_* value stops the build with its name
    clients = [
        LLMClient(
            api_key=config[key_name],
            model=model,
            base_url=_PROVIDER_SETTINGS[key_name]["base_url"],
            sampling=sampling,
        )
        for key_name, model in configured_llm_models(config)
    ]
    if not clients:
        raise RuntimeError("No LLM API key configured: expected one of " + ", ".join(_PROVIDER_PREFERENCE))
    return clients[0] if len(clients) == 1 else FailoverLLM(clients)
