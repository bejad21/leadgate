import html
import logging
from abc import ABC, abstractmethod

from engine.core import followup
from engine.core.crm_contacts import _find_or_create, find_recent_duplicate, lead_contact_values

logger = logging.getLogger(__name__)

PRICE_TOLERANCE = 0.5

# What a lead is, beyond a plain expression of interest. Each kind gets its own tag in Odoo.
KIND_TAGS = {"reservation": "Reservation", "viewing": "Viewing"}


class DomainAdapter(ABC):
    # Tools that change state. The agent loop rate-limits these per chat.
    write_tools = frozenset({"create_lead", "reserve_item", "book_viewing"})

    @abstractmethod
    def tool_schemas(self) -> list[dict]:
        """Return OpenRouter/OpenAI-format function-calling schemas for this domain's tools."""

    @abstractmethod
    def execute_tool(self, name: str, args: dict) -> dict:
        """Execute a named tool call and return a JSON-serializable result."""


def schedule_follow_up(odoo, lead_id: int, summary: str, **options) -> None:
    """Best effort: the lead already exists, so a failed reminder is logged, never raised."""
    try:
        followup.schedule_activity(odoo, lead_id, summary=summary, **options)
    except Exception:
        logger.exception("could not schedule the follow-up for lead %s", lead_id)


def create_verified_lead(
    odoo,
    domain_type: str,
    args: dict,
    *,
    kind: str = "lead",
    extra_notes: tuple[str, ...] = (),
    follow_up: dict | None = None,
) -> dict:
    """Create a crm.lead from tool arguments.

    `kind` marks reservations and viewings (tagged in Odoo); `extra_notes` are added to the
    description; `follow_up` overrides the default next-day call (summary, kind, days, due).

    The price the model passes is only trusted if a catalog item of this
    domain really has that price. Otherwise expected_revenue is left unset and
    the lead is marked, so a manipulated conversation cannot plant an
    arbitrary revenue figure in the CRM.
    """
    existing = find_recent_duplicate(odoo, args)
    if existing is not None:
        # The same customer already opened this lead moments ago (a resend after an
        # error, or the model calling the tool twice). Hand back that lead.
        return {"lead_id": existing, "duplicate": True}

    contact_values, description_parts = lead_contact_values(odoo, domain_type, args)
    if args.get("notes"):
        description_parts.insert(0, f"Notes: {args['notes']}")
    description_parts = [*extra_notes, *description_parts]
    if kind in KIND_TAGS:
        kind_tag = _find_or_create(odoo, "crm.tag", [("name", "=", KIND_TAGS[kind])], {"name": KIND_TAGS[kind]})
        if kind_tag:
            tag_ids = contact_values.get("tag_ids") or [(6, 0, [])]
            contact_values["tag_ids"] = [(6, 0, [*tag_ids[0][2], kind_tag])]

    values = {"name": args["name"], **contact_values}
    try:
        owner_id = followup.salesperson_id(odoo)
    except Exception:
        logger.exception("could not look up the salesperson; leaving the lead unassigned")
        owner_id = None
    if owner_id:
        values["user_id"] = owner_id
    price_verified = True
    price = args.get("price")
    if price:
        catalog_match = odoo.search_read(
            "leadgate.catalog.item",
            [
                ("domain_type", "=", domain_type),
                ("price", ">=", price - PRICE_TOLERANCE),
                ("price", "<=", price + PRICE_TOLERANCE),
            ],
            ["id"],
        )
        if catalog_match:
            values["expected_revenue"] = price
        else:
            price_verified = False
            description_parts.append("Price unverified: no catalog item has this price")
    # Odoo renders the description as HTML, and customer text goes into it, so each
    # line is escaped and becomes its own paragraph.
    values["description"] = "".join(f"<p>{html.escape(line, quote=False)}</p>" for line in description_parts)

    result = {"lead_id": odoo.create("crm.lead", values)}
    plan = follow_up or {"summary": f"Call {args.get('customer_name') or 'the customer'} about {args['name']}"}
    schedule_follow_up(odoo, result["lead_id"], **plan)
    if not price_verified:
        result["price_verified"] = False
        result["note"] = (
            "The price the customer stated does not match any catalog item and "
            "was not recorded. Do not repeat or confirm that price. Tell the "
            "customer a human will confirm the real price."
        )
    return result
