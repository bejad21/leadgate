import json
import math

from engine.core.actions import ACTION_TOOLS, action_tool_schemas, execute_action_tool
from engine.core.adapter_base import DomainAdapter, create_verified_lead

SORT_OPTIONS = ["price_asc", "price_desc", "mileage_asc", "year_desc"]


def _attrs(record: dict) -> dict | None:
    try:
        parsed = json.loads(record.get("attributes") or "")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _num(value) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _location_matches(query, location) -> bool:
    """A state code ("FL") must equal a whole part of "City, ST", never a
    fragment of a city name; a longer query ("Orlando") may match inside a
    part. A query that already has a comma is matched as written."""
    q = str(query).strip().lower()
    place = str(location).lower()
    if "," in q:
        return q in place
    parts = [p.strip() for p in place.split(",")]
    return any(q == p or (len(q) > 2 and q in p) for p in parts)


def _matches_attribute_filters(record: dict, args: dict) -> bool:
    attrs = _attrs(record)
    if attrs is None:
        return False
    if args.get("year_min") is not None:
        year = _num(attrs.get("year"))
        if year is None or year < args["year_min"]:
            return False
    if args.get("mileage_max") is not None:
        mileage = _num(attrs.get("mileage"))
        if mileage is None or mileage > args["mileage_max"]:
            return False
    if args.get("condition"):
        if str(attrs.get("condition", "")).lower() != str(args["condition"]).lower():
            return False
    if args.get("location"):
        if not _location_matches(args["location"], attrs.get("location", "")):
            return False
    return True


def _sort_key(sort_by: str):
    def key(record: dict):
        attrs = _attrs(record) or {}
        if sort_by in ("price_asc", "price_desc"):
            return record.get("price", 0)
        if sort_by == "mileage_asc":
            return _num(attrs.get("mileage")) if _num(attrs.get("mileage")) is not None else float("inf")
        return _num(attrs.get("year")) if _num(attrs.get("year")) is not None else 0
    return key


class CarsAdapter(DomainAdapter):
    def __init__(self, odoo_client):
        self.odoo = odoo_client

    def tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_inventory",
                    "description": "Search available cars. Every filter is optional and they combine.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "make": {"type": "string"},
                            "model": {"type": "string"},
                            "price_min": {"type": "number"},
                            "price_max": {"type": "number"},
                            "year_min": {"type": "integer", "description": "Oldest acceptable model year"},
                            "mileage_max": {"type": "number", "description": "Highest acceptable mileage"},
                            "condition": {"type": "string", "description": "New, Used, or Certified"},
                            "location": {"type": "string", "description": "City or state, e.g. Orlando or FL"},
                            "sort_by": {"type": "string", "enum": SORT_OPTIONS},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_lead",
                    "description": "Create a CRM lead for a customer interested in a specific car",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Title for the lead, e.g. the car's name",
                            },
                            "customer_name": {"type": "string"},
                            "customer_contact": {
                                "type": "string",
                                "description": "Customer's email or phone number",
                            },
                            "price": {
                                "type": "number",
                                "description": "Price of the car, used as expected revenue",
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
            *action_tool_schemas("car"),
        ]

    def execute_tool(self, name: str, args: dict) -> dict:
        if name == "search_inventory":
            odoo_domain = [("domain_type", "=", "cars"), ("status", "=", "available")]
            if args.get("make"):
                odoo_domain.append(("name", "ilike", args["make"]))
            if args.get("model"):
                odoo_domain.append(("name", "ilike", args["model"]))
            if args.get("price_min"):
                odoo_domain.append(("price", ">=", args["price_min"]))
            if args.get("price_max"):
                odoo_domain.append(("price", "<=", args["price_max"]))
            records = self.odoo.search_read("leadgate.catalog.item", odoo_domain, ["name", "price", "attributes"])
            if any(args.get(k) not in (None, "") for k in ("year_min", "mileage_max", "condition", "location")):
                records = [r for r in records if _matches_attribute_filters(r, args)]
            if args.get("sort_by") in SORT_OPTIONS:
                records = sorted(records, key=_sort_key(args["sort_by"]), reverse=args["sort_by"] in ("price_desc", "year_desc"))
            return {"matches": records[:5], "count": len(records)}
        if name == "create_lead":
            return create_verified_lead(self.odoo, "cars", args)
        if name in ACTION_TOOLS:
            return execute_action_tool(self.odoo, "cars", name, args)
        raise ValueError(f"Unknown tool: {name}")
