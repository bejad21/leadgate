from unittest.mock import MagicMock

from engine.adapters.cars import CarsAdapter


def test_search_inventory_filters_by_price():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = [
        {"name": "Toyota Corolla", "price": 18000.0, "attributes": "2019, automatic"},
        {"name": "Honda Civic", "price": 21000.0, "attributes": "2020, manual"},
    ]

    adapter = CarsAdapter(mock_odoo)
    result = adapter.execute_tool("search_inventory", {"price_max": 30000})

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "cars"),
            ("status", "=", "available"),
            ("price", "<=", 30000),
        ],
        ["name", "price", "attributes"],
    )
    assert result == {
        "matches": [
            {"name": "Toyota Corolla", "price": 18000.0, "attributes": "2019, automatic"},
            {"name": "Honda Civic", "price": 21000.0, "attributes": "2020, manual"},
        ],
        "count": 2,
    }


def test_search_inventory_without_price_max_omits_price_filter():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = []

    adapter = CarsAdapter(mock_odoo)
    adapter.execute_tool("search_inventory", {})

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "cars"),
            ("status", "=", "available"),
        ],
        ["name", "price", "attributes"],
    )


def test_create_lead_calls_odoo_create_with_crm_lead():
    mock_odoo = MagicMock()
    mock_odoo.create.return_value = 55

    adapter = CarsAdapter(mock_odoo)
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

    mock_odoo.create.assert_called_once_with(
        "crm.lead",
        {
            "name": "Toyota Corolla",
            "description": (
                "Customer: Jane Doe\nContact: jane@example.com\nNotes: Interested in test drive"
            ),
            "expected_revenue": 18000.0,
        },
    )
    assert result == {"lead_id": 55}


def test_unknown_tool_raises_value_error():
    adapter = CarsAdapter(MagicMock())
    try:
        adapter.execute_tool("not_a_tool", {})
        assert False, "expected ValueError"
    except ValueError:
        pass
