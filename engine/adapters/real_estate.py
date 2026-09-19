from engine.core.adapter_base import DomainAdapter, create_verified_lead


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
                            "price_min": {"type": "number"},
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
            if args.get("price_min"):
                odoo_domain.append(("price", ">=", args["price_min"]))
            if args.get("price_max"):
                odoo_domain.append(("price", "<=", args["price_max"]))
            records = self.odoo.search_read("leadgate.catalog.item", odoo_domain, ["name", "price", "attributes"])
            return {"matches": records[:5], "count": len(records)}
        if name == "create_lead":
            return create_verified_lead(self.odoo, "real_estate", args)
        raise ValueError(f"Unknown tool: {name}")
