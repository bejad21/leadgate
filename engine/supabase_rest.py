"""How the engine reaches Supabase: the REST address and the service key's headers.

SUPABASE_URL is the project address (https://<ref>.supabase.co). It is easy to paste the REST
address from the dashboard by mistake (.../rest/v1/), so that suffix and any trailing slash are
accepted and removed. Anything that is not an http(s) address is refused by name, never echoed.
"""
import os
from urllib.parse import urlparse


def base_url(value: str | None = None) -> str | None:
    """The project address without a trailing slash or /rest/v1, or None when SUPABASE_URL is unset.
    Raises ValueError naming the variable if it is set but is not an http(s) address."""
    raw = os.environ.get("SUPABASE_URL") if value is None else value
    if not raw or not raw.strip():
        return None
    text = raw.strip().rstrip("/")
    if text.endswith("/rest/v1"):
        text = text[: -len("/rest/v1")].rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("SUPABASE_URL must be the project address, for example https://<ref>.supabase.co")
    return text


def project_ref(value: str | None = None) -> str | None:
    """The project reference, the first label of the hostname, or None when SUPABASE_URL is unset."""
    base = base_url(value)
    return (urlparse(base).hostname or "").split(".")[0] if base else None


def service_headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
