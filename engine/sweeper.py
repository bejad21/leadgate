"""Nudge the owner about leads nobody has touched.

A lead that is still in the "New" stage after a while has had no reply and no call. The
sweeper alerts the owner once more. The record that it did so is a tag on the lead in Odoo,
not something in memory, so a restart cannot make it nudge twice. The lead is tagged before
the message goes out (at most once), and un-tagged if the message could not be sent, so a
failed send is retried on the next sweep.
"""
import asyncio
import datetime as dt
import html
import logging
from dataclasses import dataclass
from typing import Any, Callable

from engine.core.crm_contacts import _find_or_create
from engine.leads import LeadInfo
from engine.notifier import alert_keyboard

logger = logging.getLogger(__name__)

REMINDER_TAG = "Reminded"
SWEEP_INTERVAL_SECONDS = 60


@dataclass
class SweepDeps:
    odoo: Any
    store: Any
    owner: Any  # send_owner_html
    now: Callable[[], dt.datetime]


def _minutes_waiting(created: str | None, now: dt.datetime) -> int:
    try:
        started = dt.datetime.strptime(created or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return 0
    return max(0, int((now - started).total_seconds() // 60))


def sweep_once(deps: SweepDeps, minutes: int) -> int:
    """Remind about every untouched lead older than `minutes`. Returns how many reminders went out."""
    try:
        return _sweep(deps, minutes)
    except Exception:
        logger.exception("the lead sweep failed")
        return 0


def tag_as_reminded(odoo, lead_id: int) -> None:
    """Mark a lead so the sweeper leaves it alone: it has been reminded about, or the owner has it."""
    tag_id = _find_or_create(odoo, "crm.tag", [("name", "=", REMINDER_TAG)], {"name": REMINDER_TAG})
    if tag_id:
        odoo.write("crm.lead", [lead_id], {"tag_ids": [(4, tag_id)]})


def _sweep(deps: SweepDeps, minutes: int) -> int:
    odoo = deps.odoo
    now = deps.now()
    tag_id = _find_or_create(odoo, "crm.tag", [("name", "=", REMINDER_TAG)], {"name": REMINDER_TAG})
    if not tag_id:
        return 0
    cutoff = (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    leads = odoo.search_read(
        "crm.lead",
        [
            ("stage_id.name", "=", "New"),
            ("source_id.name", "=", "Telegram"),
            ("create_date", "<", cutoff),
            ("tag_ids", "not in", [tag_id]),
        ],
        ["name", "contact_name", "email_from", "phone", "create_date"],
        order="create_date asc",
        limit=10,
    )
    sent = 0
    for row in leads:
        # One lead failing must not stop the others, so each is handled on its own.
        try:
            sent += _remind(deps, row, tag_id, now)
        except Exception:
            logger.exception("could not remind about lead %s", row.get("id"))
    return sent


def _remind(deps: SweepDeps, row: dict, tag_id: int, now: dt.datetime) -> int:
    odoo, lead_id = deps.odoo, row["id"]
    esc = lambda value: html.escape(str(value or ""), quote=False)
    odoo.write("crm.lead", [lead_id], {"tag_ids": [(4, tag_id)]})  # claim it first
    try:
        try:
            chat = deps.store.get_lead_chat(lead_id)
        except Exception:
            logger.exception("could not look up the chat for lead %s; the reminder goes out without a reply button", lead_id)
            chat = None
        info = LeadInfo(
            lead_id=lead_id, domain_type="", item_name=row.get("name") or "", customer_name=row.get("contact_name") or "",
            email=row.get("email_from") or None, phone=row.get("phone") or None, price=None, price_verified=True,
            kind=(chat or {}).get("kind", "lead"),
        )
        text = (
            f"<b>Still waiting</b> #{lead_id}\n"
            f"{esc(row.get('contact_name')) or 'Unnamed customer'}: {esc(row.get('name'))}\n"
            f"No reply for {_minutes_waiting(row.get('create_date'), now)} min"
        )
        delivery = deps.owner.send_owner_html(text, buttons=alert_keyboard(info, can_reply=chat is not None))
    except Exception:
        odoo.write("crm.lead", [lead_id], {"tag_ids": [(3, tag_id)]})  # let the next sweep retry
        raise
    if not delivery.ok:
        odoo.write("crm.lead", [lead_id], {"tag_ids": [(3, tag_id)]})
        return 0
    if delivery.message_id:
        try:
            deps.store.add_alert_message(lead_id, delivery.message_id)
        except Exception:
            logger.exception("could not remember the reminder message for lead %s", lead_id)
    return 1


async def run_forever(make_deps: Callable[[], SweepDeps], minutes: int, interval: int = SWEEP_INTERVAL_SECONDS) -> None:
    """Sweep every `interval` seconds until cancelled. Runs the blocking work off the event loop."""
    while True:
        await asyncio.sleep(interval)
        try:
            deps = make_deps()
            await asyncio.to_thread(sweep_once, deps, minutes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not run the lead sweep")
