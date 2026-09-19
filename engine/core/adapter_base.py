from abc import ABC, abstractmethod

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
    description_parts = []
    if args.get("customer_name"):
        description_parts.append(f"Customer: {args['customer_name']}")
    if args.get("customer_contact"):
        description_parts.append(f"Contact: {args['customer_contact']}")
    if args.get("notes"):
        description_parts.append(f"Notes: {args['notes']}")

    values = {"name": args["name"]}
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
    values["description"] = "\n".join(description_parts)

    result = {"lead_id": odoo.create("crm.lead", values)}
    if not price_verified:
        result["price_verified"] = False
        result["note"] = (
            "The price the customer stated does not match any catalog item and "
            "was not recorded. Do not repeat or confirm that price. Tell the "
            "customer a human will confirm the real price."
        )
    return result
