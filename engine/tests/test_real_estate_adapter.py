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
    mock_odoo.create.return_value = 91

    adapter = RealEstateAdapter(mock_odoo)
    result = adapter.execute_tool(
        "create_lead",
        {
            "name": "3BR Apartment Downtown",
            "customer_name": "John Smith",
            "customer_contact": "555-1234",
            "price": 350000.0,
            "notes": "Wants a viewing this weekend",
        },
    )

    mock_odoo.create.assert_called_once_with(
        "crm.lead",
        {
            "name": "3BR Apartment Downtown",
            "description": (
                "Customer: John Smith\nContact: 555-1234\nNotes: Wants a viewing this weekend"
            ),
            "expected_revenue": 350000.0,
        },
    )
    assert result == {"lead_id": 91}


def test_unknown_tool_raises_value_error():
    adapter = RealEstateAdapter(MagicMock())
    try:
        adapter.execute_tool("not_a_tool", {})
        assert False, "expected ValueError"
    except ValueError:
        pass
