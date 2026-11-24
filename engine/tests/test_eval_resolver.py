import datetime as dt
import json

import pytest

from eval import resolver

CARS = [
    {"id": 10, "name": "2021 Land Rover Range Rover", "price": 34726.0,
     "attributes": json.dumps({"make": "Land Rover", "model": "Range Rover", "year": "2021", "location": "Cincinnati, OH"})},
    {"id": 11, "name": "2021 Land Rover Range Rover", "price": 40470.0,
     "attributes": json.dumps({"make": "Land Rover", "model": "Range Rover", "year": "2021", "location": "Cincinnati, OH"})},
    {"id": 12, "name": "2021 Land Rover Range Rover", "price": 40470.0,
     "attributes": json.dumps({"make": "Land Rover", "model": "Range Rover", "year": "2021", "location": "Austin, TX"})},
    {"id": 13, "name": "2024 Toyota Camry", "price": 32550.0,
     "attributes": json.dumps({"make": "Toyota", "model": "Camry", "year": "2024", "location": "Clarksville, IN"})},
]
HOMES = [
    {"id": 50, "name": "2bd/2ba residential - Philadelphia, Pennsylvania", "price": 200000.0,
     "attributes": json.dumps({"bed": "2", "bath": "2", "city": "Philadelphia", "state": "Pennsylvania"})},
    {"id": 51, "name": "3bd/2ba residential - Philadelphia, Pennsylvania", "price": 239000.0,
     "attributes": json.dumps({"bed": "3", "bath": "2", "city": "Philadelphia", "state": "Pennsylvania"})},
    {"id": 52, "name": "4bd/4ba residential - Bryn Mawr, Pennsylvania", "price": 1279000.0,
     "attributes": json.dumps({"bed": "4", "bath": "4", "city": "Bryn Mawr", "state": "Pennsylvania"})},
]
REF = dt.date(2026, 11, 4)  # a Wednesday


def test_a_car_is_found_by_price_and_place_when_the_price_alone_is_ambiguous():
    text = "<catalog id of the 2021 Land Rover Range Rover priced 40470 in Cincinnati, OH>"
    assert resolver.resolve_value(text, CARS, REF) == 11


def test_a_car_is_found_by_year_model_and_price():
    assert resolver.resolve_value("<catalog id of the 2024 Toyota Camry priced 32550 in Clarksville, IN>", CARS, REF) == 13


def test_a_home_is_found_by_beds_baths_city_and_price():
    assert resolver.resolve_value("<catalog id of the 2 bed 2 bath Philadelphia listing priced 200000>", HOMES, REF) == 50
    assert resolver.resolve_value("<catalog id of the 4 bed 4 bath Bryn Mawr listing priced 1279000>", HOMES, REF) == 52


def test_an_ambiguous_description_is_an_error_not_a_guess():
    with pytest.raises(resolver.ResolutionError, match="2 items"):
        resolver.resolve_value("<catalog id of the 2021 Land Rover Range Rover priced 40470>", CARS, REF)


def test_a_description_that_matches_nothing_is_an_error():
    with pytest.raises(resolver.ResolutionError, match="no catalog item"):
        resolver.resolve_value("<catalog id of the 2019 Honda Odyssey priced 32900>", CARS, REF)


def test_the_first_weekday_after_the_reference_date():
    assert resolver.resolve_value("<the first Saturday after the harness reference date, YYYY-MM-DD>", [], REF) == "2026-11-07"
    assert resolver.resolve_value("<the first Friday after the harness reference date, YYYY-MM-DD>", [], REF) == "2026-11-06"
    assert resolver.resolve_value("<the first Saturday after the harness reference date>", [], REF) == "2026-11-07"


def test_the_weekday_after_a_matching_weekday_is_a_week_later_not_today():
    saturday = dt.date(2026, 11, 7)
    assert resolver.resolve_value("<the first Saturday after the harness reference date>", [], saturday) == "2026-11-14"


def test_values_that_are_not_placeholders_pass_through():
    assert resolver.resolve_value("Grace Kim", CARS, REF) == "Grace Kim"
    assert resolver.resolve_value(40470, CARS, REF) == 40470
    assert resolver.resolve_value("<something unknown>", CARS, REF) == "<something unknown>"


def test_resolve_args_reports_each_failure_and_resolves_the_rest():
    args = {"item_id": "<catalog id of the 2019 Honda Odyssey priced 32900>", "customer_name": "Grace Kim"}
    resolved, errors = resolver.resolve_args(args, CARS, REF)
    assert resolved["customer_name"] == "Grace Kim"
    assert "item_id" not in resolved
    assert len(errors) == 1 and "item_id" in errors[0]


def test_a_weekday_matcher_replaces_the_date_placeholder_when_the_rule_table_asks_for_any_such_day():
    matcher = resolver.weekday_matcher("Saturday", REF)
    assert matcher == {"$weekday_after": {"weekday": 5, "after": "2026-11-04"}}


def test_a_weekday_marker_in_the_rule_table_resolves_to_the_any_such_day_matcher():
    assert resolver.resolve_value("$weekday:Friday", [], REF) == {"$weekday_after": {"weekday": 4, "after": "2026-11-04"}}
