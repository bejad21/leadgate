from typing import Annotated, Literal, get_args

from pydantic import Field

from engine.core.actions import ACTION_TOOLS, execute_action_tool, shared_tools
from engine.core.adapter_base import UNAVAILABLE_NOTE, DomainAdapter, create_verified_lead, unavailable_matches
from engine.core.tools import OptionalNumber, OptionalText, Tool, ToolArgs, optional_choice

# A listing's name ends with its city and the full state name ("... - Marlborough, Connecticut"), but a customer, and so the
# model, often writes the two-letter code ("Marlborough, CT").
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}


def location_terms(location: str) -> list[str]:
    """The words a listing's name must contain for a location such as "Bristol, CT": the city and the full state name.
    Each comma-separated part is one term, and a bare two-letter part is read as a state code."""
    terms = []
    for part in str(location).split(","):
        part = part.strip().strip(".")
        if not part:
            continue
        terms.append(STATE_NAMES.get(part.upper(), part) if len(part) == 2 else part)
    return terms


SortBy = Literal["price_asc", "price_desc"]
SORT_OPTIONS = list(get_args(SortBy))


class RealEstateSearchArgs(ToolArgs):
    price_min: OptionalNumber = None
    price_max: OptionalNumber = None
    location: OptionalText = Field(None, description="City or state name, e.g. Marlborough or Connecticut")
    sort_by: Annotated[SortBy | None, optional_choice(SORT_OPTIONS)] = None


class RealEstateAdapter(DomainAdapter):
    domain_type = "real_estate"
    crm_tag = "Real estate"

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "search_listings",
                "Search available real estate listings by minimum and maximum price and by city or state, optionally cheapest or dearest first",
                RealEstateSearchArgs,
            ),
            *shared_tools("listing"),
        ]

    def execute_tool(self, name: str, args: dict) -> dict:
        if name == "search_listings":
            base_domain = [("domain_type", "=", self.domain_type)]
            for term in location_terms(args.get("location") or ""):
                base_domain.append(("name", "ilike", term))  # a listing's name ends with its city and state
            if args.get("price_min"):
                base_domain.append(("price", ">=", args["price_min"]))
            if args.get("price_max"):
                base_domain.append(("price", "<=", args["price_max"]))
            available_domain = [base_domain[0], ("status", "=", "available"), *base_domain[1:]]
            records = self.odoo.search_read("leadgate.catalog.item", available_domain, ["name", "price", "attributes"])
            if args.get("sort_by") in SORT_OPTIONS:
                records = sorted(records, key=lambda r: r.get("price", 0), reverse=args["sort_by"] == "price_desc")
            result = {"matches": records[:5], "count": len(records)}
            if location_terms(args.get("location") or ""):  # a named place is how a customer asks about one listing
                held = unavailable_matches(self.odoo, base_domain)
                if held:
                    result["unavailable"] = held
                    result["note"] = UNAVAILABLE_NOTE
            return result
        if name == "create_lead":
            return create_verified_lead(self.odoo, self.domain_type, args, crm_tag=self.crm_tag)
        if name in ACTION_TOOLS:
            return execute_action_tool(self.odoo, self.domain_type, name, args, crm_tag=self.crm_tag)
        raise ValueError(f"Unknown tool: {name}")
