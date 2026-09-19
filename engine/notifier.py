"""Tell the owner when a lead arrives, through a separate Telegram bot.

The alert bot is not the customer bot: it has its own token, it only ever
messages the owner's chat, and customers cannot see it. Configure it with
TELEGRAM_ALERTS_BOT_TOKEN and TELEGRAM_ALERTS_CHAT_ID. If either is missing the
alert is skipped, so the engine runs the same without it.

Sending is best-effort. An alert that fails must never break the customer's
reply or lose the lead, so every failure is logged and reported as False.
"""
import html
import logging
import os

import httpx

from engine.leads import LeadInfo

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 5


def _lead_url(lead_id: int) -> str:
    base = (os.environ.get("ODOO_PUBLIC_URL") or os.environ.get("ODOO_URL") or "").rstrip("/")
    return f"{base}/odoo/action-crm.crm_lead_all_leads/{lead_id}"


def format_alert(lead: LeadInfo) -> str:
    """The alert text, in Telegram's HTML subset. Everything a customer typed is
    escaped, so a name like "<b>Boss</b>" arrives as text, not markup."""
    esc = html.escape
    lines = [f"<b>New lead</b> #{lead.lead_id}", f"<b>{esc(lead.item_name, quote=False)}</b>"]

    who = esc(lead.customer_name, quote=False) or "Unnamed customer"
    contact = " · ".join(esc(part, quote=False) for part in (lead.email, lead.phone) if part)
    lines.append(f"{who}")
    lines.append(contact or "No contact given")

    if lead.price is not None:
        if lead.price_verified:
            lines.append(f"${lead.price:,.0f}")
        else:
            lines.append(f"Price not verified (customer said ${lead.price:,.0f})")

    url = esc(_lead_url(lead.lead_id), quote=True)
    lines.append(f'<a href="{url}">Open in Odoo</a>')
    return "\n".join(lines)


def send_lead_alert(lead: LeadInfo) -> bool:
    token = os.environ.get("TELEGRAM_ALERTS_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_ALERTS_CHAT_ID")
    if not token or not chat_id:
        logger.debug("alert bot not configured; skipping alert for lead %s", lead.lead_id)
        return False
    try:
        response = httpx.post(
            f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": format_alert(lead),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        logger.exception("could not reach Telegram to alert about lead %s", lead.lead_id)
        return False
    if response.status_code != 200:
        logger.error("alert for lead %s rejected by Telegram: HTTP %s", lead.lead_id, response.status_code)
        return False
    return True
