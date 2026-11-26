from unittest.mock import MagicMock

import pytest

from engine.adapters.real_estate import RealEstateAdapter


def test_search_listings_filters_by_price():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = [
        {"name": "3BR Apartment Downtown", "price": 350000.0, "attributes": "3 bedrooms, apartment"},
        {"name": "2BR Condo Uptown", "price": 280000.0, "attributes": "2 bedrooms, condo"},
    ]

    adapter = RealEstateAdapter(mock_odoo)
    result = adapter.execute_tool(
        "search_listings",
        {"price_max": 400000},
    )

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "real_estate"),
            ("status", "=", "available"),
            ("price", "<=", 400000),
        ],
        ["name", "price", "attributes"],
    )
    assert result == {
        "matches": [
            {"name": "3BR Apartment Downtown", "price": 350000.0, "attributes": "3 bedrooms, apartment"},
            {"name": "2BR Condo Uptown", "price": 280000.0, "attributes": "2 bedrooms, condo"},
        ],
        "count": 2,
    }


def test_search_listings_without_price_max_omits_price_filter():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = []

    adapter = RealEstateAdapter(mock_odoo)
    adapter.execute_tool("search_listings", {})

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "real_estate"),
            ("status", "=", "available"),
        ],
        ["name", "price", "attributes"],
    )


def test_create_lead_calls_odoo_create_with_crm_lead():
    mock_odoo = MagicMock()
    # Only the catalog check finds anything; contact, source and tag lookups come back empty.
    mock_odoo.search_read.side_effect = lambda model, domain, fields: (
        [{"name": "Toyota Corolla", "attributes": "{}"}] if model == "leadgate.catalog.item" else []
    )
    mock_odoo.create.side_effect = [10, 11, 12, 55]  # partner, source, tag, lead

    adapter = RealEstateAdapter(mock_odoo)
    result = adapter.execute_tool(
        "create_lead",
        {
            "name": "Toyota Corolla",
            "customer_name": "Jane Doe",
            "customer_contact": "jane@example.com",
            "price": 18000.0,
            "notes": "Interested in test drive",
        },
    )

    created = [(c.args[0], c.args[1]) for c in mock_odoo.create.call_args_list]
    assert created[0] == ("res.partner", {"name": "Jane Doe", "email": "jane@example.com"})
    assert created[1] == ("utm.source", {"name": "Telegram"})
    assert created[2] == ("crm.tag", {"name": "Real estate"})
    model, lead = created[3]
    assert model == "crm.lead"
    assert lead["name"] == "Toyota Corolla"
    assert lead["contact_name"] == "Jane Doe"
    assert lead["email_from"] == "jane@example.com"
    assert lead["partner_id"] == 10 and lead["source_id"] == 11 and lead["tag_ids"] == [(6, 0, [12])]
    assert lead["description"] == "<p>Notes: Interested in test drive</p>"
    assert lead["expected_revenue"] == 18000.0
    assert result == {"lead_id": 55}


def test_unknown_tool_raises_value_error():
    adapter = RealEstateAdapter(MagicMock())
    with pytest.raises(ValueError):
        adapter.execute_tool("not_a_tool", {})


def test_price_min_becomes_an_odoo_domain_clause():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = []
    RealEstateAdapter(odoo).execute_tool("search_listings", {"price_min": 300000, "price_max": 500000})
    domain = odoo.search_read.call_args[0][1]
    assert ("price", ">=", 300000) in domain
    assert ("price", "<=", 500000) in domain


def _search_schema():
    return RealEstateAdapter(MagicMock()).tool_schemas()[0]


def test_search_schema_declares_only_the_filters_that_work():
    assert set(_search_schema()["function"]["parameters"]["properties"]) == {"price_min", "price_max", "location", "sort_by"}


def test_search_description_promises_nothing_it_does_not_filter_on():
    description = _search_schema()["function"]["description"].lower()

    assert "bedroom" not in description
    assert "property type" not in description
    assert "price" in description


def test_an_argument_the_schema_does_not_declare_is_dropped_by_validation():
    from engine.core.tools import parse_tool_args

    model = RealEstateAdapter(MagicMock()).tool_models()["search_listings"]
    cleaned = parse_tool_args(model, {"bedrooms": 3, "property_type": "condo", "price_max": 400000})

    assert cleaned == {"price_max": 400000}


def test_schema_advertises_price_min():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    props = RealEstateAdapter(MagicMock()).tool_schemas()[0]["function"]["parameters"]["properties"]
    assert "price_min" in props



def test_lead_price_is_kept_when_it_matches_a_catalog_item():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = [{"name": "X", "attributes": "{}"}]  # the item the lead names
    odoo.create.return_value = 9
    RealEstateAdapter(odoo).execute_tool("create_lead", {"name": "X", "customer_name": "Sam", "price": 21834})
    verify_domain = [c[0][1] for c in odoo.search_read.call_args_list if c[0][0] == "leadgate.catalog.item"][0]
    assert ("domain_type", "=", "real_estate") in verify_domain
    values = [c[0][1] for c in odoo.create.call_args_list if c[0][0] == "crm.lead"][0]
    assert values["expected_revenue"] == 21834
    assert "unverified" not in values["description"].lower()


def test_lead_price_is_dropped_when_no_catalog_item_has_it():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = []
    odoo.create.return_value = 9
    RealEstateAdapter(odoo).execute_tool("create_lead", {"name": "X", "customer_name": "Sam", "price": 1})
    values = [c[0][1] for c in odoo.create.call_args_list if c[0][0] == "crm.lead"][0]
    assert "expected_revenue" not in values
    assert "price unverified" in values["description"].lower()


def test_lead_result_flags_an_unverified_price_so_the_model_can_say_so():
    from unittest.mock import MagicMock

    from engine.core.adapter_base import create_verified_lead

    odoo = MagicMock()
    odoo.search_read.return_value = []
    odoo.create.return_value = 9
    result = create_verified_lead(odoo, "cars", {"name": "X", "customer_name": "Sam", "price": 1}, crm_tag="Cars")
    assert result["lead_id"] == 9
    assert result["price_verified"] is False
    assert "do not" in result["note"].lower()


def test_lead_result_marks_a_verified_price():
    from unittest.mock import MagicMock

    from engine.core.adapter_base import create_verified_lead

    odoo = MagicMock()
    odoo.search_read.return_value = [{"name": "X", "attributes": "{}"}]  # the item the lead names
    odoo.create.return_value = 9
    result = create_verified_lead(odoo, "cars", {"name": "X", "customer_name": "Sam", "price": 21834}, crm_tag="Cars")
    assert result == {"lead_id": 9}


def _three_homes():
    return [
        {"name": "B", "price": 300000.0, "attributes": "{}"},
        {"name": "C", "price": 500000.0, "attributes": "{}"},
        {"name": "A", "price": 120000.0, "attributes": "{}"},
    ]


def test_search_listings_can_sort_cheapest_first_across_every_match():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = _three_homes()

    result = RealEstateAdapter(mock_odoo).execute_tool("search_listings", {"sort_by": "price_asc"})

    assert [m["name"] for m in result["matches"]] == ["A", "B", "C"]
    assert result["count"] == 3


def test_search_listings_can_sort_dearest_first():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = _three_homes()

    result = RealEstateAdapter(mock_odoo).execute_tool("search_listings", {"sort_by": "price_desc"})

    assert [m["name"] for m in result["matches"]] == ["C", "B", "A"]


def test_the_sort_comes_before_the_five_result_cut():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = [{"name": f"H{i}", "price": float(1000 - i), "attributes": "{}"} for i in range(8)]

    result = RealEstateAdapter(mock_odoo).execute_tool("search_listings", {"sort_by": "price_asc"})

    assert [m["price"] for m in result["matches"]] == [993.0, 994.0, 995.0, 996.0, 997.0]
    assert result["count"] == 8


def test_the_search_schema_offers_only_the_two_price_sorts():
    schema = next(t for t in RealEstateAdapter(MagicMock()).tool_schemas() if t["function"]["name"] == "search_listings")
    assert schema["function"]["parameters"]["properties"]["sort_by"]["enum"] == ["price_asc", "price_desc"]


def _odoo_with(available, unavailable):
    from unittest.mock import MagicMock

    odoo = MagicMock()
    odoo.search_read.side_effect = lambda model, domain, fields, **kw: unavailable if ("status", "in", ["reserved", "sold"]) in domain else available
    return odoo


def test_a_listing_can_be_looked_up_by_city_or_state():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = []
    RealEstateAdapter(odoo).execute_tool("search_listings", {"location": "Marlborough", "price_max": 300000})
    domain = odoo.search_read.call_args_list[0].args[1]
    assert ("name", "ilike", "Marlborough") in domain and ("price", "<=", 300000) in domain and ("status", "=", "available") in domain


def test_a_named_listing_that_is_on_hold_or_sold_is_reported_as_such():
    from engine.adapters.real_estate import RealEstateAdapter

    held = [{"name": "3bd/2ba residential - Marlborough, Connecticut", "status": "reserved", "attributes": ""}]
    result = RealEstateAdapter(_odoo_with([], held)).execute_tool("search_listings", {"location": "Marlborough"})
    assert result["matches"] == [] and result["unavailable"] == [{"name": "3bd/2ba residential - Marlborough, Connecticut", "status": "on hold"}]


def test_a_price_only_home_search_does_not_look_for_unavailable_listings():
    from engine.adapters.real_estate import RealEstateAdapter

    odoo = _odoo_with([{"name": "x", "price": 1.0, "attributes": ""}], [{"name": "y", "status": "sold", "attributes": ""}])
    result = RealEstateAdapter(odoo).execute_tool("search_listings", {"price_max": 500000})
    assert "unavailable" not in result and odoo.search_read.call_count == 1


def test_a_state_code_is_read_as_the_full_state_name():
    from engine.adapters.real_estate import location_terms

    assert location_terms("Bristol, CT") == ["Bristol", "Connecticut"]
    assert location_terms("CT") == ["Connecticut"]
    assert location_terms("nj") == ["New Jersey"]
    assert location_terms("Marlborough") == ["Marlborough"]
    assert location_terms("New Jersey") == ["New Jersey"]
    assert location_terms("Mays Landing, New Jersey") == ["Mays Landing", "New Jersey"]
    assert location_terms("  ,  ") == []


def test_a_city_and_state_code_search_finds_the_listing_by_both_words():
    from unittest.mock import MagicMock

    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = []
    RealEstateAdapter(odoo).execute_tool("search_listings", {"location": "Bristol, CT"})
    domain = odoo.search_read.call_args_list[0].args[1]
    assert ("name", "ilike", "Bristol") in domain and ("name", "ilike", "Connecticut") in domain
