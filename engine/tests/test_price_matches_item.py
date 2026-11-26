"""A lead's price is recorded only when the item the lead names has that price. A real price from another item used to
pass: "the 2020 Camry for $1,500" was accepted if anything in the catalog cost $1,500."""
import json

import pytest

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.tests.fakes import FakeOdoo, item


class PriceOdoo(FakeOdoo):
    def search_read(self, model, domain, fields, **kwargs):
        rows = super().search_read(model, domain, fields, **kwargs)
        if model == "leadgate.catalog.item":
            for clause in domain:
                if isinstance(clause, tuple) and clause[0] == "price":
                    low = clause[1] == ">="
                    rows = [r for r in rows if (r["price"] >= clause[2] if low else r["price"] <= clause[2])]
        return rows


def row(item_id, name, price, domain_type="cars", **attributes):
    record = item(item_id, name=name, price=price, domain_type=domain_type)
    record["attributes"] = json.dumps({k: str(v) for k, v in attributes.items()})
    return record


def cars():
    rows = [
        row(1, "2020 Toyota Camry", 21834.0, make="Toyota", model="Camry", year=2020),
        row(2, "2012 Kia Rio", 1500.0, make="Kia", model="Rio", year=2012),
        row(3, "2021 Ford F150", 33000.0, make="Ford", model="F150", year=2021),
    ]
    return CarsAdapter(PriceOdoo(items={r["id"]: r for r in rows}))


def homes():
    rows = [
        row(10, "3bd/2ba residential - Guilford, Maine", 379000.0, "real_estate", city="Guilford", state="Maine", bed=3, bath=2),
        row(11, "5bd/5ba residential - Eliot, Maine", 850000.0, "real_estate", city="Eliot", state="Maine", bed=5, bath=5),
    ]
    return RealEstateAdapter(PriceOdoo(items={r["id"]: r for r in rows}))


def lead(adapter, name, price):
    result = adapter.execute_tool("create_lead", {"name": name, "customer_name": "Sam", "customer_contact": "sam@example.com", "price": price})
    values = next(v for model, v in adapter.odoo.created if model == "crm.lead")
    return result, values


@pytest.mark.parametrize("name", ["2020 Toyota Camry", "Toyota Camry", "Camry enquiry - Sam", "Lead: 2020 Camry"])
def test_the_named_cars_own_price_is_recorded(name):
    result, values = lead(cars(), name, 21834)
    assert values["expected_revenue"] == 21834 and "price_verified" not in result, name


def test_hyphens_and_spaces_in_the_model_name_do_not_matter():
    assert lead(cars(), "2021 Ford F-150", 33000)[1]["expected_revenue"] == 33000


@pytest.mark.parametrize("name, price", [
    ("2020 Toyota Camry", 1500),      # the Rio's price
    ("2019 Toyota Camry", 21834),     # another model year
    ("2022 Tesla Model Y", 33000),    # a car the catalog does not have, at another car's price
])
def test_another_items_price_is_not_recorded(name, price):
    result, values = lead(cars(), name, price)
    assert "expected_revenue" not in values and result["price_verified"] is False, name
    assert "Price unverified" in values["description"]


@pytest.mark.parametrize("name", ["3bd/2ba house in Guilford, Maine", "Guilford 3 bed", "3bd/2ba residential - Guilford, Maine"])
def test_the_named_homes_own_price_is_recorded(name):
    assert lead(homes(), name, 379000)[1]["expected_revenue"] == 379000, name


@pytest.mark.parametrize("name", ["Interest in a car", "Listing", "Vehicle purchase enquiry", "$21,834 car", "Lead"])
def test_a_title_that_names_no_item_keeps_a_price_the_catalog_has(name):
    """Nothing to mismatch: such a title says only that the customer wants something at that price."""
    assert lead(cars(), name, 21834)[1]["expected_revenue"] == 21834, name


def test_a_title_that_names_no_item_still_needs_a_price_the_catalog_has():
    assert "expected_revenue" not in lead(cars(), "Listing", 1)[1]


@pytest.mark.parametrize("name", ["$379,000 property", "House", "3 bed home enquiry"])
def test_a_home_title_that_names_no_place_keeps_a_catalog_price(name):
    assert lead(homes(), name, 379000)[1]["expected_revenue"] == 379000, name


@pytest.mark.parametrize("name", ["Guilford Twp house", "House in Guilford at $379,000"])
def test_the_way_a_town_is_written_may_differ_a_little(name):
    assert lead(homes(), name, 379000)[1]["expected_revenue"] == 379000, name


def test_a_township_in_the_catalog_matches_the_plain_town_name():
    township = row(12, "4bd/4ba residential - Berkeley Heights Twp, New Jersey", 1199000.0, "real_estate",
                   city="Berkeley Heights Twp", state="New Jersey", bed=4, bath=4)
    adapter = RealEstateAdapter(PriceOdoo(items={12: township}))
    assert lead(adapter, "Berkeley Heights house at $1,199,000", 1199000)[1]["expected_revenue"] == 1199000


@pytest.mark.parametrize("name, price", [
    ("House in Eliot", 379000), ("5 bed house in Guilford", 379000), ("Montauk house, offer of $379,000", 379000),
])
def test_another_homes_price_is_not_recorded(name, price):
    result, values = lead(homes(), name, price)
    assert "expected_revenue" not in values and result["price_verified"] is False, name


def test_a_hold_records_the_catalog_price_of_the_held_item():
    adapter = cars()
    adapter.execute_tool("reserve_item", {"item_id": 1, "customer_name": "Sam", "customer_contact": "sam@example.com"})
    values = next(v for model, v in adapter.odoo.created if model == "crm.lead")
    assert values["expected_revenue"] == 21834
