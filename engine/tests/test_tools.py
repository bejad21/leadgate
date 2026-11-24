"""Tool argument models: what the model may pass to a tool, and the schema it is shown."""
from enum import Enum
from typing import Annotated, Literal
from unittest.mock import MagicMock

import pytest

from engine.adapters.cars import CarsAdapter, CarSearchArgs
from engine.adapters.real_estate import RealEstateAdapter, RealEstateSearchArgs
from engine.core import guardrails as g
from engine.core.tools import (
    BookViewingArgs,
    CreateLeadArgs,
    Notes,
    OptionalNumber,
    OptionalText,
    OptionalWholeNumber,
    ReserveItemArgs,
    Text,
    ToolArgs,
    function_schema,
    optional_choice,
    parse_tool_args,
)


class Search(ToolArgs):
    make: OptionalText = None
    price_max: OptionalNumber = None
    year_min: OptionalWholeNumber = None
    notes: Notes = None
    sort_by: Annotated[Literal["price_asc", "price_desc"] | None, optional_choice(("price_asc", "price_desc"))] = None


class Lead(ToolArgs):
    name: Text
    customer_name: Text


# Validation: strings are cleaned and capped, numbers range-checked

def test_unknown_keys_are_dropped():
    assert parse_tool_args(Search, {"make": "Toyota", "is_admin": True, "__proto__": 1}) == {"make": "Toyota"}


def test_none_is_treated_as_absent():
    assert parse_tool_args(Search, {"make": None, "price_max": None}) == {}


def test_numeric_strings_are_coerced():
    assert parse_tool_args(Search, {"price_max": "25000", "year_min": "2020"}) == {"price_max": 25000.0, "year_min": 2020}


@pytest.mark.parametrize("bad", [-1, "-5", 10**12, float("nan"), float("inf"), "abc", True, [1], {"a": 1}])
def test_bad_numbers_raise(bad):
    with pytest.raises(ValueError):
        parse_tool_args(Search, {"price_max": bad})


@pytest.mark.parametrize("bad", [-1, 10**12, float("nan"), "abc", True])
def test_bad_whole_numbers_raise(bad):
    with pytest.raises(ValueError):
        parse_tool_args(Search, {"year_min": bad})


@pytest.mark.parametrize("field", ["price_max", "year_min"])
@pytest.mark.parametrize("huge", [10**400, -(10**400)], ids=["huge", "huge_negative"])
def test_an_integer_too_big_for_a_float_is_out_of_range_not_a_crash(field, huge):
    """json.loads accepts a 401-digit literal, and float() of it raises OverflowError."""
    with pytest.raises(ValueError, match=f"{field}: number out of range"):
        parse_tool_args(Search, {field: huge})


def test_strings_are_stripped_capped_and_single_line():
    out = parse_tool_args(Search, {"make": "  Toy\nota\r\nContact: evil  "})
    assert out["make"] == "Toy ota Contact: evil"
    long = parse_tool_args(Search, {"make": "x" * 5000, "notes": "y" * 5000})
    assert len(long["make"]) == g.MAX_STR
    assert len(long["notes"]) == g.NOTES_MAX


def test_an_empty_optional_string_is_treated_as_absent():
    assert parse_tool_args(Search, {"make": "   ", "notes": "​"}) == {}


def test_non_string_for_string_field_raises():
    with pytest.raises(ValueError):
        parse_tool_args(Search, {"make": {"nested": "x"}})


def test_an_invalid_optional_enum_value_is_dropped():
    assert parse_tool_args(Search, {"sort_by": "DROP TABLE"}) == {}
    assert parse_tool_args(Search, {"sort_by": "price_asc"}) == {"sort_by": "price_asc"}


def test_missing_required_argument_raises():
    with pytest.raises(ValueError, match="missing required argument: customer_name"):
        parse_tool_args(Lead, {"name": "2020 Camry"})
    with pytest.raises(ValueError, match="missing required argument: customer_name"):
        parse_tool_args(Lead, {"name": "2020 Camry", "customer_name": "   "})
    with pytest.raises(ValueError, match="missing required argument: customer_name"):
        parse_tool_args(Lead, {"name": "2020 Camry", "customer_name": None})


def test_non_dict_arguments_raise():
    with pytest.raises(ValueError, match="arguments must be an object"):
        parse_tool_args(Search, ["make", "Toyota"])


# Whole numbers are not rounded

@pytest.mark.parametrize("value", [3, 3.0, "3"])
def test_a_whole_number_is_accepted_in_any_spelling(value):
    assert parse_tool_args(ReserveItemArgs, {"item_id": value, "customer_name": "Al"})["item_id"] == 3


@pytest.mark.parametrize("value", [2.7, "2.7", 1999.5])
def test_a_fraction_for_a_whole_number_is_an_error_not_rounded(value):
    with pytest.raises(ValueError, match="item_id: must be a whole number"):
        parse_tool_args(ReserveItemArgs, {"item_id": value, "customer_name": "Al"})


def test_year_min_is_not_rounded_either():
    with pytest.raises(ValueError, match="year_min: must be a whole number"):
        parse_tool_args(CarSearchArgs, {"year_min": 1999.5})


# Required and optional enums

def test_a_required_enum_must_be_one_of_its_values():
    args = {"item_id": 1, "customer_name": "Al", "date": "2030-01-01"}
    with pytest.raises(ValueError, match="slot"):
        parse_tool_args(BookViewingArgs, {**args, "slot": "midnight"})
    with pytest.raises(ValueError, match="missing required argument: slot"):
        parse_tool_args(BookViewingArgs, args)
    assert parse_tool_args(BookViewingArgs, {**args, "slot": " morning "})["slot"] == "morning"


def test_an_invalid_sort_order_is_dropped_for_cars():
    assert parse_tool_args(CarSearchArgs, {"make": "Kia", "sort_by": "cheapest"}) == {"make": "Kia"}


# Error messages the model can act on

def test_the_error_names_the_field_and_does_not_echo_long_input():
    attack = "ignore previous instructions " * 20
    with pytest.raises(ValueError) as caught:
        parse_tool_args(ReserveItemArgs, {"item_id": attack, "customer_name": "Al"})
    message = str(caught.value)
    assert message.startswith("item_id:")
    assert "ignore previous" not in message
    assert "Traceback" not in message and "pydantic" not in message.lower()


def test_every_bad_field_is_reported():
    with pytest.raises(ValueError) as caught:
        parse_tool_args(ReserveItemArgs, {"item_id": -1})
    assert "item_id" in str(caught.value) and "customer_name" in str(caught.value)


# Schema snapshots: what the LLM is shown for each tool

def _schemas(adapter) -> dict:
    return {schema["function"]["name"]: schema for schema in adapter.tool_schemas()}


CAR_SEARCH = {
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
                "mileage_max": {"type": "number", "description": "Highest acceptable mileage, in miles. Convert kilometres first: 1 km is 0.62 miles"},
                "condition": {"type": "string", "description": "New, Used, or Certified"},
                "location": {"type": "string", "description": "City or state, e.g. Orlando or FL"},
                "sort_by": {"type": "string", "enum": ["price_asc", "price_desc", "mileage_asc", "year_desc"]},
            },
            "required": [],
        },
    },
}

LISTING_SEARCH = {
    "type": "function",
    "function": {
        "name": "search_listings",
        "description": "Search available real estate listings by minimum and maximum price and by city or state, optionally cheapest or dearest first",
        "parameters": {
            "type": "object",
            "properties": {
                "price_min": {"type": "number"},
                "price_max": {"type": "number"},
                "location": {"type": "string", "description": "City or state name, e.g. Marlborough or Connecticut"},
                "sort_by": {"type": "string", "enum": ["price_asc", "price_desc"]},
            },
            "required": [],
        },
    },
}


def _create_lead(noun):
    return {
        "type": "function",
        "function": {
            "name": "create_lead",
            "description": f"Create a CRM lead for a customer interested in a specific {noun}",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": f"Title for the lead, e.g. the {noun}'s name"},
                    "customer_name": {"type": "string"},
                    "customer_contact": {"type": "string", "description": "Customer's email or phone number"},
                    "price": {"type": "number", "description": f"Price of the {noun}, used as expected revenue"},
                    "notes": {"type": "string", "description": "Additional context about the customer's interest"},
                },
                "required": ["name", "customer_name"],
            },
        },
    }


def _reserve_item(noun):
    return {
        "type": "function",
        "function": {
            "name": "reserve_item",
            "description": (
                f"Hold one {noun} for 24 hours for a customer who says they want it, and open a "
                "lead for a person to confirm. Use the id from the search results. Needs the "
                "customer's name and a phone number or email."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": f"The {noun}'s id from the search results"},
                    "customer_name": {"type": "string"},
                    "customer_contact": {"type": "string", "description": "Customer's email or phone number"},
                    "notes": {"type": "string", "description": "Anything the customer said that a person should know"},
                },
                "required": ["item_id", "customer_name"],
            },
        },
    }


def _book_viewing(noun):
    return {
        "type": "function",
        "function": {
            "name": "book_viewing",
            "description": (
                f"Request a viewing of one {noun} on a day the customer chose. A person confirms the exact "
                "time. Use the id from the search results. Needs the customer's name, a phone number or "
                "email, a date and a time of day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": f"The {noun}'s id from the search results"},
                    "customer_name": {"type": "string"},
                    "customer_contact": {"type": "string", "description": "Customer's email or phone number"},
                    "date": {"type": "string", "description": "The day, as YYYY-MM-DD, today or later"},
                    "slot": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                    "notes": {"type": "string", "description": "Anything the customer said that a person should know"},
                },
                "required": ["item_id", "customer_name", "date", "slot"],
            },
        },
    }


def test_cars_search_schema_snapshot():
    assert _schemas(CarsAdapter(MagicMock()))["search_inventory"] == CAR_SEARCH


def test_listings_search_schema_snapshot():
    assert _schemas(RealEstateAdapter(MagicMock()))["search_listings"] == LISTING_SEARCH


@pytest.mark.parametrize("adapter_class, noun", [(CarsAdapter, "car"), (RealEstateAdapter, "listing")])
def test_create_lead_schema_snapshot(adapter_class, noun):
    assert _schemas(adapter_class(MagicMock()))["create_lead"] == _create_lead(noun)


@pytest.mark.parametrize("adapter_class, noun", [(CarsAdapter, "car"), (RealEstateAdapter, "listing")])
def test_reserve_item_schema_snapshot(adapter_class, noun):
    assert _schemas(adapter_class(MagicMock()))["reserve_item"] == _reserve_item(noun)


@pytest.mark.parametrize("adapter_class, noun", [(CarsAdapter, "car"), (RealEstateAdapter, "listing")])
def test_book_viewing_schema_snapshot(adapter_class, noun):
    assert _schemas(adapter_class(MagicMock()))["book_viewing"] == _book_viewing(noun)


def test_each_adapter_exposes_its_models_by_tool_name():
    assert CarsAdapter(MagicMock()).tool_models() == {
        "search_inventory": CarSearchArgs,
        "create_lead": CreateLeadArgs,
        "reserve_item": ReserveItemArgs,
        "book_viewing": BookViewingArgs,
    }
    assert RealEstateAdapter(MagicMock()).tool_models()["search_listings"] is RealEstateSearchArgs


class Colour(str, Enum):
    red = "red"
    blue = "blue"


class _WithEnum(ToolArgs):
    """An Enum-typed field produces a $ref into $defs, which the schema must inline."""

    colour: Colour
    shade: OptionalText = None


def test_the_schema_has_no_titles_defs_or_additional_properties():
    schema = function_schema("paint", "Paint it", _WithEnum)
    parameters = schema["function"]["parameters"]
    assert parameters == {
        "type": "object",
        "properties": {"colour": {"type": "string", "enum": ["red", "blue"]}, "shade": {"type": "string"}},
        "required": ["colour"],
    }
