import html
from abc import ABC, abstractmethod

from engine.core.crm_contacts import find_recent_duplicate, lead_contact_values

PRICE_TOLERANCE = 0.5


class DomainAdapter(ABC):
    # Tools that change state. The agent loop rate-limits these per chat.
    write_tools = frozenset({"create_lead"})

    @abstractmethod
    def tool_schemas(self) -> list[dict]:
        """Return OpenRouter/OpenAI-format function-calling schemas for this domain's tools."""

    @abstractmethod
    def execute_tool(self, name: str, args: dict) -> dict:
        """Execute a named tool call and return a JSON-serializable result."""


def create_verified_lead(odoo, domain_type: str, args: dict) -> dict:
    """Create a crm.lead from tool arguments.

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

    values = {"name": args["name"], **contact_values}
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
    if not price_verified:
        result["price_verified"] = False
        result["note"] = (
            "The price the customer stated does not match any catalog item and "
            "was not recorded. Do not repeat or confirm that price. Tell the "
            "customer a human will confirm the real price."
        )
    return result
