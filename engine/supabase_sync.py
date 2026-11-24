"""Mirror leads and conversation turns into Supabase so the dashboard can show them.

The engine's own state (the chat history it replays, lead-to-chat map) is in other tables the browser
cannot read; the dashboard reads these two tables instead. They are private: only staff accounts can
read them (see migrations/0002_leads_and_conversations.sql), and this module writes with the service
key, which never leaves the server.

A chat is identified by a keyed hash (HMAC) of its Telegram chat id, never the id
itself. Telegram ids are small numbers, so an unkeyed or default-salted hash could
be reversed by trying every id. Without CHAT_REF_SECRET nothing is written.

Every call is best-effort: it returns False on any problem and never raises, so the
customer's reply is never held up or lost by the dashboard being unreachable.
"""
import hashlib
import hmac
import logging
import os

import httpx

from engine import supabase_rest
from engine.leads import LeadInfo

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5

# Where a lead stands, as the dashboard shows it. A status outside this list is refused.
STATUSES = frozenset({"new", "taken", "contacted", "confirmed", "won", "lost", "released"})


MIN_SECRET_LENGTH = 16


class MissingSecret(RuntimeError):
    """CHAT_REF_SECRET is not set, so chats cannot be referenced safely."""


def chat_ref(chat_id: int) -> str:
    secret = os.environ.get("CHAT_REF_SECRET")
    # A short secret could be guessed, and the hash then reversed by trying every chat id, so it counts as missing.
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise MissingSecret(f"set CHAT_REF_SECRET (a random string of at least {MIN_SECRET_LENGTH} characters) to mirror conversations")
    return hmac.new(secret.encode(), str(chat_id).encode(), hashlib.sha256).hexdigest()[:12]


def _post(table: str, body: dict, *, upsert_on: str | None = None) -> bool:
    base = supabase_rest.base_url()
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not base or not key:
        logger.debug("Supabase not configured; skipping write to %s", table)
        return False
    url = f"{base}/rest/v1/{table}"
    headers = {**supabase_rest.service_headers(key), "Prefer": "return=minimal"}
    params = {}
    if upsert_on:
        params["on_conflict"] = upsert_on
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    try:
        response = httpx.post(url, headers=headers, params=params, json=body, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        logger.exception("could not reach Supabase for %s", table)
        return False
    if response.status_code not in (200, 201, 204):
        logger.error("Supabase rejected a write to %s: HTTP %s", table, response.status_code)
        return False
    return True


def _ref_or_none(chat_id: int) -> str | None:
    try:
        return chat_ref(chat_id)
    except MissingSecret:
        logger.error("CHAT_REF_SECRET is not set; not mirroring to Supabase")
        return None


def record_lead(chat_id: int, lead: LeadInfo) -> bool:
    ref = _ref_or_none(chat_id)
    if ref is None:
        return False
    return _post(
        "leads",
        {
            "odoo_lead_id": lead.lead_id,
            "domain_type": lead.domain_type,
            "item_name": lead.item_name,
            "customer_name": lead.customer_name or None,
            "email": lead.email,
            "phone": lead.phone,
            "price": lead.price,
            "price_verified": lead.price_verified,
            "kind": lead.kind,
            "detail": lead.detail,
            "chat_ref": ref,
            # no "status": the upsert merges, and a re-sent lead must not become "new" again
        },
        upsert_on="odoo_lead_id",
    )


def record_turn(chat_id: int, domain_type: str, message: str, reply: str, tool_calls: list, blocked: bool = False) -> bool:
    ref = _ref_or_none(chat_id)
    if ref is None:
        return False
    return _post(
        "conversation_turns",
        {
            "chat_ref": ref,
            "domain_type": domain_type,
            "user_message": message,
            "reply": reply,
            "tool_calls": [{"name": getattr(c, "name", None), "arguments": getattr(c, "arguments", None)} for c in tool_calls],
            "blocked": blocked,
        },
    )


def update_lead(odoo_lead_id: int, fields: dict) -> bool:
    """Patch fields of a lead on the dashboard; a `status` must be one of STATUSES. Best-effort, like every call here."""
    status = fields.get("status")
    if status is not None and status not in STATUSES:
        logger.error("refusing to mirror an unknown status %r", status)
        return False
    base = supabase_rest.base_url()
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not base or not key:
        return False
    url = f"{base}/rest/v1/leads"
    headers = {**supabase_rest.service_headers(key), "Prefer": "return=minimal"}
    try:
        response = httpx.patch(url, headers=headers, params={"odoo_lead_id": f"eq.{int(odoo_lead_id)}"}, json=fields, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        logger.exception("could not reach Supabase to update lead %s", odoo_lead_id)
        return False
    if response.status_code not in (200, 204):
        logger.error("Supabase rejected the update of lead %s: HTTP %s", odoo_lead_id, response.status_code)
        return False
    return True
