"""Odoo tells the engine when a catalog item's status changes; the engine updates the dashboard's row.

Odoo calls POST /webhook/odoo-status from an automated action (odoo/addons/leadgate_domain) with a
shared secret in a header. This module holds everything except the route itself: reading the body
under a size cap, validating the event, and applying it to Supabase's catalog_items through the
database function in migrations/0004 (atomic, idempotent, ignores events older than the stored write
time), with retries on transient failures.

An event carries the item's id, domain, name, price, new status and the time Odoo wrote it. None of it
is personal data, and none of it, nor the secret, is written to the log: a line names the item id and
the outcome only.
"""
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from fastapi import Request

from engine import supabase_rest

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Odoo-Webhook-Secret"
MIN_SECRET_LENGTH = 16
MAX_BODY_BYTES = 8 * 1024
STATUSES = frozenset({"available", "reserved", "sold"})
MAX_ID = 2**53
# Odoo and the engine may disagree a little about the time; further ahead than this is not a clock, it is a mistake.
MAX_FUTURE = timedelta(hours=1)

ATTEMPTS = 4
BACKOFF_SECONDS = (0.25, 0.5, 1.0)
REQUEST_TIMEOUT_SECONDS = 5.0


class BodyTooLarge(Exception):
    pass


class InvalidEvent(ValueError):
    """The body is not a status event. The message says what is wrong, never what was sent."""


class SyncFailed(RuntimeError):
    """Supabase did not take the event, even after retries. The message names no address or key."""


@dataclass(frozen=True)
class StatusEvent:
    odoo_id: int
    domain_type: str
    name: str
    price: float
    status: str
    written_at: datetime


def secret_problem(secret: str | None) -> str | None:
    """Why ODOO_WEBHOOK_SECRET cannot protect the route, or None. The route answers 503 until it can."""
    if not secret:
        return "ODOO_WEBHOOK_SECRET is not set, so the Odoo status webhook is switched off"
    if len(secret) < MIN_SECRET_LENGTH:
        return f"ODOO_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters, so the Odoo status webhook is switched off"
    return None


async def read_body(request: Request) -> bytes:
    """The request body, refusing more than MAX_BODY_BYTES without reading the rest."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise BodyTooLarge
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            raise BodyTooLarge
    return bytes(received)


def _number(value, *, what: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise InvalidEvent(f"{what} must be a number from 0 to {maximum:g}")
    return float(value)


def parse_write_time(value, now: datetime | None = None) -> datetime:
    """Odoo's write time as an aware UTC datetime. Odoo sends 'YYYY-MM-DD HH:MM:SS' in UTC; an ISO 8601
    time with an offset is accepted too. Raises InvalidEvent if it is neither, or lies in the future."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidEvent("write_date must be a time")
    text = value.strip().replace(" ", "T", 1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidEvent("write_date must be a time like 2026-09-20 12:00:00") from None
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    if parsed > (now or datetime.now(timezone.utc)) + MAX_FUTURE:
        raise InvalidEvent("write_date is in the future")
    return parsed


def parse_event(body, now: datetime | None = None) -> StatusEvent:
    """Validate a decoded JSON body. Raises InvalidEvent."""
    if not isinstance(body, dict):
        raise InvalidEvent("the event must be a JSON object")
    odoo_id = body.get("id")
    if isinstance(odoo_id, bool) or not isinstance(odoo_id, int) or not 0 < odoo_id < MAX_ID:
        raise InvalidEvent("id must be a positive whole number")
    domain_type = body.get("domain_type")
    if not isinstance(domain_type, str) or not (0 < len(domain_type) <= 40) or not domain_type.replace("_", "").isalnum():
        raise InvalidEvent("domain_type must be a short word")
    status = body.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise InvalidEvent(f"status must be one of {', '.join(sorted(STATUSES))}")
    name = body.get("name")
    if not isinstance(name, str) or len(name) > 300:
        raise InvalidEvent("name must be text of at most 300 characters")
    price = _number(body.get("price"), what="price", maximum=1e10)
    return StatusEvent(odoo_id, domain_type, name, price, status, parse_write_time(body.get("write_date"), now))


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)


def _transient(error: httpx.HTTPError | None, status: int | None) -> bool:
    return error is not None or status == 429 or (status is not None and status >= 500)


def apply_event(
    event: StatusEvent,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Apply the event to catalog_items. Returns 'inserted', 'updated', 'unchanged' or 'stale'.

    Network errors, 429 and 5xx are retried with a growing pause, up to ATTEMPTS times; any other refusal
    is final. Raises SyncFailed when the event could not be applied."""
    base = supabase_rest.base_url()
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not base or not key:
        raise SyncFailed("Supabase is not configured")
    url = f"{base}/rest/v1/rpc/apply_catalog_event"
    payload = {
        "p_odoo_id": event.odoo_id,
        "p_domain_type": event.domain_type,
        "p_name": event.name,
        "p_price": event.price,
        "p_status": event.status,
        "p_written_at": event.written_at.isoformat(),
    }
    own_client = client is None
    http = client or _new_client()
    error: httpx.HTTPError | None = None
    status: int | None = None
    try:
        for attempt in range(ATTEMPTS):
            error, response = None, None
            try:
                response = http.post(url, headers=supabase_rest.service_headers(key), json=payload)
            except httpx.HTTPError as exc:
                error = exc
            status = response.status_code if response is not None else None
            if response is not None and response.status_code < 300:
                outcome = response.json()
                if outcome not in ("inserted", "updated", "unchanged", "stale"):
                    raise SyncFailed("Supabase answered something unexpected")
                return outcome
            if not _transient(error, status) or attempt == ATTEMPTS - 1:
                break
            logger.warning("odoo status event for item %s: Supabase did not take it (%s); retrying", event.odoo_id, _why(error, status))
            (sleep or time.sleep)(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        raise SyncFailed(f"Supabase did not take the event ({_why(error, status)})")
    finally:
        if own_client:
            http.close()


def _why(error: httpx.HTTPError | None, status: int | None) -> str:
    return type(error).__name__ if error is not None else f"HTTP {status}"


def process(event: StatusEvent) -> None:
    """Apply an event and log the outcome (item id and result only). Never raises: it runs after the response."""
    try:
        outcome = apply_event(event)
    except SyncFailed as exc:
        logger.error("odoo status event for item %s was not applied: %s", event.odoo_id, exc)
    except Exception as exc:
        logger.error("odoo status event for item %s was not applied: %s", event.odoo_id, type(exc).__name__)
    else:
        logger.info("odoo status event for item %s: %s", event.odoo_id, outcome)
