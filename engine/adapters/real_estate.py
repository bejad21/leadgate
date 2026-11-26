import json
import logging
import re
from typing import Annotated, Literal, get_args

from pydantic import Field

from engine.core.actions import ACTION_TOOLS, execute_action_tool, shared_tools
from engine.core.adapter_base import UNAVAILABLE_NOTE, DomainAdapter, create_verified_lead, unavailable_matches
from engine.core.paging import OFFSET_ARGUMENT, PAGE_SIZE
from engine.core.tools import OptionalNumber, OptionalText, Tool, ToolArgs, optional_choice

logger = logging.getLogger(__name__)

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


def _attrs(row: dict) -> dict:
    try:
        parsed = json.loads(row.get("attributes") or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _word_pattern(words: str) -> str:
    return rf"(?<!\w){re.escape(words)}(?!\w)"


def places_named(text: str, cities: set[str]) -> list[str]:
    """The catalog cities a message names. A city is taken when it is written as the catalog writes it ("Eliot"), or in
    any case next to a word that makes it a place ("the eliot house", "in eliot"), so that a town called "Home" is not
    read into "a home". A city inside a longer one that matched ("York" in "New York") is dropped."""
    found = []
    for city in cities:
        word = _word_pattern(city)
        placed = rf"(?:(?<!\w)(?:in|at|near)\s+{word}|{word}\s+(?:house|home|property|listing|place|one)(?!\w))"
        if re.search(word, text) or re.search(placed, text, re.IGNORECASE):
            found.append(city)
    return [c for c in found if not any(c != other and c.lower() in other.lower() for other in found)]


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
    generic_title_words = frozenset(
        {"house", "home", "property", "residential", "estate", "bd", "ba", "bed", "beds", "bedroom", "bath", "baths", "bathroom"}
    )

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "search_listings",
                "Search available real estate listings by minimum and maximum price and by city or state, optionally cheapest or dearest first",
                RealEstateSearchArgs,
            ),
            *shared_tools("listing"),
        ]

    def complete_search(self, name: str, arguments: dict, customer_text: str) -> dict:
        """Add the city when the message names exactly one catalog city and the search has no place. Without it the
        search cannot report that the listing asked about is on hold or sold."""
        if name != "search_listings" or location_terms(str(arguments.get("location") or "")):
            return arguments
        try:
            rows = self.odoo.search_read("leadgate.catalog.item", [("domain_type", "=", self.domain_type)], ["attributes"])
        except Exception:
            logger.warning("could not read the catalog's cities; searching without the place", exc_info=True)
            return arguments
        cities = {str(city) for row in rows if (city := _attrs(row).get("city"))}
        named = places_named(customer_text, cities)
        return {**arguments, "location": named[0]} if len(named) == 1 else arguments

    def lead_names_item(self, lead_name: str, row: dict) -> bool:
        """A home is named by its city. A bedroom count written in the title must be the home's count."""
        if super().lead_names_item(lead_name, row):
            return True
        attrs = _attrs(row)
        city = re.sub(r"\s+(?:twp|township|boro|borough)\.?$", "", str(attrs.get("city") or ""), flags=re.IGNORECASE)
        if not city or not re.search(_word_pattern(city), lead_name, re.IGNORECASE):
            return False
        beds = re.findall(r"(\d+)\s*-?\s*(?:bd|bed|br)(?![a-z])|(\d+)\s*-?\s*bedroom", lead_name, re.IGNORECASE)
        counts = {a or b for a, b in beds}
        return not counts or str(attrs.get("bed")) in counts

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
            offset = args.get(OFFSET_ARGUMENT, 0)  # set by the engine for the "Next 5" button only
            result = {"matches": records[offset : offset + PAGE_SIZE], "count": len(records)}
            if location_terms(args.get("location") or ""):  # a named place is how a customer asks about one listing
                held = unavailable_matches(self.odoo, base_domain)
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
