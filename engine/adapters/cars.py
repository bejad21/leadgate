import json
import math
import re
from typing import Annotated, Literal, get_args

from pydantic import Field

from engine.core.actions import ACTION_TOOLS, execute_action_tool, shared_tools
from engine.core.adapter_base import UNAVAILABLE_NOTE, DomainAdapter, create_verified_lead, squash, unavailable_matches
from engine.core.paging import OFFSET_ARGUMENT, PAGE_SIZE
from engine.core.tools import OptionalNumber, OptionalText, OptionalWholeNumber, Tool, ToolArgs, optional_choice

SortBy = Literal["price_asc", "price_desc", "mileage_asc", "year_desc"]
SORT_OPTIONS = list(get_args(SortBy))


class CarSearchArgs(ToolArgs):
    make: OptionalText = None
    model: OptionalText = None
    price_min: OptionalNumber = None
    price_max: OptionalNumber = None
    year_min: OptionalWholeNumber = Field(None, description="Oldest acceptable model year")
    mileage_max: OptionalNumber = Field(
        None, description="Highest acceptable mileage, in miles. Convert kilometres first: 1 km is 0.62 miles"
    )
    condition: OptionalText = Field(None, description="New, Used, or Certified")
    location: OptionalText = Field(None, description="City or state, e.g. Orlando or FL")
    sort_by: Annotated[SortBy | None, optional_choice(SORT_OPTIONS)] = None


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


_STATE_CODES = {
    name.lower(): code
    for name, code in (
        ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"), ("California", "CA"),
        ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"), ("Florida", "FL"), ("Georgia", "GA"),
        ("Hawaii", "HI"), ("Idaho", "ID"), ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"),
        ("Kansas", "KS"), ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
        ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"), ("Mississippi", "MS"),
        ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"), ("New Hampshire", "NH"),
        ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"), ("North Carolina", "NC"),
        ("North Dakota", "ND"), ("Ohio", "OH"), ("Oklahoma", "OK"), ("Oregon", "OR"), ("Pennsylvania", "PA"),
        ("Rhode Island", "RI"), ("South Carolina", "SC"), ("South Dakota", "SD"), ("Tennessee", "TN"),
        ("Texas", "TX"), ("Utah", "UT"), ("Vermont", "VT"), ("Virginia", "VA"), ("Washington", "WA"),
        ("West Virginia", "WV"), ("Wisconsin", "WI"), ("Wyoming", "WY"), ("District of Columbia", "DC"),
    )
}


def _location_matches(query, location) -> bool:
    """A state code ("FL") must equal a whole part of "City, ST", never a
    fragment of a city name; a longer query ("Orlando") may match inside a
    part. A full state name ("Florida") is read as its code, and also still
    as a city word, so "Washington" finds both Seattle, WA and Washington, DC.
    A query that already has a comma is matched as written, with the state
    after the comma read the same way."""
    q = str(query).strip().lower()
    place = str(location).lower()
    if "," in q:
        city, _, state = (part.strip() for part in q.partition(","))
        state = _STATE_CODES.get(state, state).lower()
        return f"{city}, {state}" in place
    parts = [p.strip() for p in place.split(",")]
    code = _STATE_CODES.get(q)
    if code and code.lower() in parts:
        return True
    return any(q == p or (len(q) > 2 and q in p) for p in parts)


def _name_matches(record: dict, args: dict) -> bool:
    """The make and model a customer wrote appear in the item's name, whatever the hyphens and spaces."""
    name = squash(record.get("name", ""))
    return all(squash(args[key]) in name for key in ("make", "model") if args.get(key))


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
            mileage = _num(attrs.get("mileage"))
            return mileage if mileage is not None else float("inf")
        year = _num(attrs.get("year"))
        return year if year is not None else 0
    return key


class CarsAdapter(DomainAdapter):
    domain_type = "cars"
    crm_tag = "Cars"
    generic_title_words = frozenset({"car", "cars", "vehicle", "auto"})

    def tools(self) -> list[Tool]:
        return [
            Tool("search_inventory", "Search available cars. Every filter is optional and they combine.", CarSearchArgs),
            *shared_tools("car"),
        ]

    def lead_names_item(self, lead_name: str, row: dict) -> bool:
        """A car is named by its model ("Camry"). A model year written in the title must be the car's year."""
        if super().lead_names_item(lead_name, row):
            return True
        attrs = _attrs(row) or {}
        model = squash(attrs.get("model"))
        if not model or model not in squash(lead_name):
            return False
        years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", lead_name))
        return not years or str(attrs.get("year")) in years

    def execute_tool(self, name: str, args: dict) -> dict:
        if name == "search_inventory":
            base_domain = [("domain_type", "=", self.domain_type)]
            if args.get("make"):
                base_domain.append(("name", "ilike", args["make"]))
            if args.get("model"):
                base_domain.append(("name", "ilike", args["model"]))
            if args.get("price_min"):
                base_domain.append(("price", ">=", args["price_min"]))
            if args.get("price_max"):
                base_domain.append(("price", "<=", args["price_max"]))
            available_domain = [base_domain[0], ("status", "=", "available"), *base_domain[1:]]
            records = self.odoo.search_read("leadgate.catalog.item", available_domain, ["name", "price", "attributes"])
            attribute_filtered = any(args.get(k) not in (None, "") for k in ("year_min", "mileage_max", "condition", "location"))
            if not records and (args.get("make") or args.get("model")):
                # Nothing contains the make and model as typed ("F-150" against an item named "F150"): read again
                # without the name conditions and compare with the hyphens and spaces left out.
                loose = [d for d in available_domain if d[0] != "name"]
                records = [r for r in self.odoo.search_read("leadgate.catalog.item", loose, ["name", "price", "attributes"]) if _name_matches(r, args)]
            if attribute_filtered:
                records = [r for r in records if _matches_attribute_filters(r, args)]
            if args.get("sort_by") in SORT_OPTIONS:
                records = sorted(records, key=_sort_key(args["sort_by"]), reverse=args["sort_by"] in ("price_desc", "year_desc"))
            offset = args.get(OFFSET_ARGUMENT, 0)  # set by the engine for the "Next 5" button only
            result = {"matches": records[offset : offset + PAGE_SIZE], "count": len(records)}
            if args.get("make") or args.get("model"):  # a named car is the one a customer asks about by status
                held = unavailable_matches(
                    self.odoo,
                    base_domain,
                    lambda r: not attribute_filtered or _matches_attribute_filters(r, args),
                )
                if held:
                    result["unavailable"] = held
                    result["note"] = UNAVAILABLE_NOTE
            return result
        if name == "create_lead":
            return create_verified_lead(
                self.odoo, self.domain_type, args, crm_tag=self.crm_tag,
                names_item=self.lead_names_item, names_no_item=self.title_names_no_item,
            )
        if name in ACTION_TOOLS:
            return execute_action_tool(self.odoo, self.domain_type, name, args, crm_tag=self.crm_tag)
        raise ValueError(f"Unknown tool: {name}")
