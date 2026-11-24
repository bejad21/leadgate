"""Point the alert bot at this engine, so its buttons and replies reach it.

    python -m engine.register_alert_webhook https://your-public-address.example
    python -m engine.register_alert_webhook --remove

The address must be public HTTPS (a Cloudflare quick tunnel works). Telegram sends the
secret in a header on every update, and the engine refuses updates without it. Only message
and button-press updates are requested; nothing else is delivered.
"""
import os
import sys
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_BASE = "https://api.telegram.org"


def _token_and_secret() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN")
    secret = os.environ.get("TELEGRAM_ALERTS_WEBHOOK_SECRET")
    if not token or not secret:
        print("Set TELEGRAM_ALERTS_BOT_TOKEN and TELEGRAM_ALERTS_WEBHOOK_SECRET in .env first.")
        return None
    return token, secret


def register(public_url: str) -> bool:
    parsed = urlparse(public_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        print("The address must be public HTTPS, for example https://something.trycloudflare.com")
        return False
    creds = _token_and_secret()
    if creds is None:
        return False
    token, secret = creds
    response = httpx.post(
        f"{TELEGRAM_API_BASE}/bot{token}/setWebhook",
        json={
            "url": f"{parsed.scheme}://{parsed.netloc}/webhook/alerts",
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=20,
    )
    ok = response.status_code == 200 and response.json().get("ok") is True
    print("Registered." if ok else f"Telegram refused it: {response.text}")
    return ok


def remove() -> bool:
    token = os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_ALERTS_BOT_TOKEN in .env first.")
        return False
    response = httpx.post(f"{TELEGRAM_API_BASE}/bot{token}/deleteWebhook", json={}, timeout=20)
    ok = response.status_code == 200 and response.json().get("ok") is True
    print("Removed." if ok else f"Telegram refused it: {response.text}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--remove":
        sys.exit(0 if remove() else 1)
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if register(sys.argv[1]) else 1)
