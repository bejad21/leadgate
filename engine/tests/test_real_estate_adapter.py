from unittest.mock import MagicMock

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
        {"property_type": "apartment", "bedrooms": 3, "price_max": 400000},
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
    mock_odoo.search_read.side_effect = lambda model, domain, fields: [{"id": 1}] if model == "leadgate.catalog.item" else []
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
    try:
        adapter.execute_tool("not_a_tool", {})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_price_min_becomes_an_odoo_domain_clause():
    from unittest.mock import MagicMock
    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = []
    RealEstateAdapter(odoo).execute_tool("search_listings", {"price_min": 300000, "price_max": 500000})
    domain = odoo.search_read.call_args[0][1]
    assert ("price", ">=", 300000) in domain
    assert ("price", "<=", 500000) in domain


def test_schema_advertises_price_min():
    from unittest.mock import MagicMock
    from engine.adapters.real_estate import RealEstateAdapter

    props = RealEstateAdapter(MagicMock()).tool_schemas()[0]["function"]["parameters"]["properties"]
    assert "price_min" in props



def test_lead_price_is_kept_when_it_matches_a_catalog_item():
    from unittest.mock import MagicMock
    from engine.adapters.real_estate import RealEstateAdapter

    odoo = MagicMock()
    odoo.search_read.return_value = [{"id": 1}]
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
    result = create_verified_lead(odoo, "cars", {"name": "X", "customer_name": "Sam", "price": 1})
    assert result["lead_id"] == 9
    assert result["price_verified"] is False
    assert "do not" in result["note"].lower()


def test_lead_result_marks_a_verified_price():
    from unittest.mock import MagicMock
    from engine.core.adapter_base import create_verified_lead

    odoo = MagicMock()
    odoo.search_read.return_value = [{"id": 1}]
    odoo.create.return_value = 9
    result = create_verified_lead(odoo, "cars", {"name": "X", "customer_name": "Sam", "price": 21834})
    assert result == {"lead_id": 9}
