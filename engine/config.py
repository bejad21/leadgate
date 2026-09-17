import os

from dotenv import load_dotenv

REQUIRED_VARS = [
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_USER",
    "ODOO_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
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
