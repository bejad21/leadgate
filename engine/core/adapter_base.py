import html
import logging
from abc import ABC, abstractmethod

from engine.core import followup
from engine.core.crm_contacts import _find_or_create, find_recent_duplicate, lead_contact_values
from engine.core.tools import Tool, ToolArgs

logger = logging.getLogger(__name__)

PRICE_TOLERANCE = 0.5

# What a lead is, beyond a plain expression of interest. Each kind gets its own tag in Odoo.
KIND_TAGS = {"reservation": "Reservation", "viewing": "Viewing"}


UNAVAILABLE_NOTE = (
    "The items under unavailable match the search but are on hold or sold. Say so if the customer asks about one "
    "of them. Never offer one as available and never try to hold or book one."
)
_STATUS_WORDS = {"reserved": "on hold", "sold": "sold"}


def unavailable_matches(odoo, domain: list, keep=lambda row: True, limit: int = 3) -> list[dict]:
    """Up to `limit` items that fit the customer's search but are on hold or sold, as {name, status}. Search only
    shows items for sale, so without this the assistant cannot tell a customer why a car they ask about is missing.
    `domain` is the search without any status condition; `keep` applies the filters Odoo cannot."""
    rows = odoo.search_read(
        "leadgate.catalog.item", [*domain, ("status", "in", list(_STATUS_WORDS))], ["name", "status", "attributes"],
        limit=500, order="status asc, id asc",  # cars on hold come first: they are the ones a customer is waiting on
    )
    return [
        {"name": row["name"], "status": _STATUS_WORDS[row["status"]]} for row in rows if row.get("status") in _STATUS_WORDS and keep(row)
    ][:limit]


class DomainAdapter(ABC):
    """One catalog. Each subclass sets `domain_type` (the catalog item rows it owns in Odoo)
    and `crm_tag` (the tag its leads get in the CRM)."""

    domain_type: str
    crm_tag: str
    # Tools that change state. The agent loop rate-limits these per chat.
    write_tools = frozenset({"create_lead", "reserve_item", "book_viewing"})

    def __init_subclass__(cls, **kwargs):
        """A concrete adapter without its domain_type or crm_tag would write untagged leads, or
        read the wrong catalog, without any error. Refuse it when the class is defined."""
        super().__init_subclass__(**kwargs)
        # ABCMeta has not filled in cls.__abstractmethods__ yet at this point, so look for
        # base methods that are still abstract by hand. A partial base is exempt.
        if any(getattr(getattr(cls, name), "__isabstractmethod__", False) for name in DomainAdapter.__abstractmethods__):
            return
        missing = [
            attribute for attribute in ("domain_type", "crm_tag")
            if not isinstance(getattr(cls, attribute, None), str) or not getattr(cls, attribute).strip()
        ]
        if missing:
            raise TypeError(f"{cls.__name__} must set a non-empty {' and '.join(missing)}")

    def __init__(self, odoo):
        self.odoo = odoo

    @abstractmethod
    def tools(self) -> list[Tool]:
        """This domain's tools, each with the model that defines its arguments."""

    def tool_schemas(self) -> list[dict]:
        """OpenRouter/OpenAI-format function-calling schemas, generated from the argument models."""
        return [tool.schema() for tool in self.tools()]

    def tool_models(self) -> dict[str, type[ToolArgs]]:
        """The argument model of each tool, by tool name. The agent loop validates with these."""
        return {tool.name: tool.args for tool in self.tools()}

    @abstractmethod
    def execute_tool(self, name: str, args: dict) -> dict:
        """Execute a named tool call and return a JSON-serializable result."""

    def get_item(self, item_id: int) -> dict | None:
        """One catalog row of this domain by id, with its status, or None. Used when a customer taps an item's button, to
        show it as it is now: an item that was held or sold since the list was sent must not be offered again."""
        rows = self.odoo.search_read(
            "leadgate.catalog.item",
            [("id", "=", int(item_id)), ("domain_type", "=", self.domain_type)],
            ["name", "price", "status", "attributes"],
        )
        return rows[0] if rows else None


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
    crm_tag: str | None,
    kind: str = "lead",
    extra_notes: tuple[str, ...] = (),
    follow_up: dict | None = None,
) -> dict:
    """Create a crm.lead from tool arguments.

    `crm_tag` is the adapter's catalog tag; `kind` marks reservations and viewings (tagged in
    Odoo); `extra_notes` are added to the description; `follow_up` overrides the default
    next-day call (summary, kind, days, due).

    The price the model passes is only trusted if a catalog item of this
    domain has that price. Otherwise expected_revenue is left unset and
    the lead is marked, so a manipulated conversation cannot plant an
    arbitrary revenue figure in the CRM.
    """
    existing = find_recent_duplicate(odoo, args)
    if existing is not None:
        # The same customer already opened this lead moments ago (a resend after an
        # error, or the model calling the tool twice). Hand back that lead.
        return {"lead_id": existing, "duplicate": True}

    contact_values, description_parts = lead_contact_values(odoo, args, crm_tag=crm_tag)
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
