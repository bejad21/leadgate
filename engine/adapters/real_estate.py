from engine.core.adapter_base import DomainAdapter


class RealEstateAdapter(DomainAdapter):
    def __init__(self, odoo_client):
        self.odoo = odoo_client

    def tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_listings",
                    "description": "Search available real estate listings by property type, bedrooms, and max price",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "property_type": {"type": "string"},
                            "bedrooms": {"type": "integer"},
                            "price_max": {"type": "number"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_lead",
                    "description": "Create a CRM lead for a customer interested in a specific listing",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Title for the lead, e.g. the listing's name",
                            },
                            "customer_name": {"type": "string"},
                            "customer_contact": {
                                "type": "string",
                                "description": "Customer's email or phone number",
                            },
                            "price": {
                                "type": "number",
                                "description": "Price of the listing, used as expected revenue",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Additional context about the customer's interest",
                            },
                        },
                        "required": ["name", "customer_name"],
                    },
                },
            },
        ]

    def execute_tool(self, name: str, args: dict) -> dict:
        if name == "search_listings":
            odoo_domain = [("domain_type", "=", "real_estate"), ("status", "=", "available")]
            if args.get("price_max"):
                odoo_domain.append(("price", "<=", args["price_max"]))
            records = self.odoo.search_read("leadgate.catalog.item", odoo_domain, ["name", "price", "attributes"])
            return {"matches": records[:5], "count": len(records)}
        if name == "create_lead":
            description_parts = []
            if args.get("customer_name"):
                description_parts.append(f"Customer: {args['customer_name']}")
            if args.get("customer_contact"):
                description_parts.append(f"Contact: {args['customer_contact']}")
            if args.get("notes"):
                description_parts.append(f"Notes: {args['notes']}")

            values = {
                "name": args["name"],
                "description": "\n".join(description_parts),
            }
            if args.get("price"):
                values["expected_revenue"] = args["price"]

            lead_id = self.odoo.create("crm.lead", values)
            return {"lead_id": lead_id}
        raise ValueError(f"Unknown tool: {name}")
