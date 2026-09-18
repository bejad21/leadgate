import os
from unittest.mock import MagicMock

import pytest

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


def test_search_inventory_filters_by_make():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = []

    adapter = CarsAdapter(mock_odoo)
    adapter.execute_tool("search_inventory", {"make": "Toyota"})

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "cars"),
            ("status", "=", "available"),
            ("name", "ilike", "Toyota"),
        ],
        ["name", "price", "attributes"],
    )


def test_search_inventory_filters_by_model():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = []

    adapter = CarsAdapter(mock_odoo)
    adapter.execute_tool("search_inventory", {"model": "Corolla"})

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "cars"),
            ("status", "=", "available"),
            ("name", "ilike", "Corolla"),
        ],
        ["name", "price", "attributes"],
    )


def test_search_inventory_filters_by_make_model_and_price_max_together():
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = []

    adapter = CarsAdapter(mock_odoo)
    adapter.execute_tool(
        "search_inventory", {"make": "Toyota", "model": "Corolla", "price_max": 25000}
    )

    mock_odoo.search_read.assert_called_once_with(
        "leadgate.catalog.item",
        [
            ("domain_type", "=", "cars"),
            ("status", "=", "available"),
            ("name", "ilike", "Toyota"),
            ("name", "ilike", "Corolla"),
            ("price", "<=", 25000),
        ],
        ["name", "price", "attributes"],
    )


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


def _live_odoo_client():
    """Build a real OdooClient against the local Odoo instance, or skip.

    This is an integration-style check (no mocking): it proves the make/model
    ilike filter actually narrows results against the real 350-car seeded
    dataset, not just that the domain list was constructed correctly.
    """
    from engine.odoo_client import OdooClient

    url = os.environ.get("ODOO_URL", "http://localhost:8069")
    db = os.environ.get("ODOO_DB", "leadgate")
    user = os.environ.get("ODOO_USER", "admin")
    password = os.environ.get("ODOO_PASSWORD", "admin")
    try:
        client = OdooClient(url, db, user, password)
        if not client.uid:
            pytest.skip("Could not authenticate against live Odoo instance")
        return client
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live Odoo instance not reachable: {exc}")


def test_live_search_inventory_make_filter_only_returns_matching_make():
    """Integration check against the real, live Odoo instance (350 seeded cars).

    Confirms search_inventory({"make": "Toyota", "price_max": ...}) only
    returns cars whose name actually contains "Toyota" -- proving the fix
    works end-to-end against real data, not just against a mock.
    """
    client = _live_odoo_client()
    adapter = CarsAdapter(client)

    result = adapter.execute_tool("search_inventory", {"make": "Toyota", "price_max": 40000})

    assert result["count"] > 0, "expected at least one live Toyota match under 40000"
    for record in result["matches"]:
        assert "toyota" in record["name"].lower(), (
            f"non-Toyota record leaked into make-filtered results: {record['name']}"
        )
        assert record["price"] <= 40000

    # Sanity check: without the make filter, the live dataset has strictly
    # more matches under the same price cap, proving the filter is genuinely
    # narrowing the query rather than passing through by coincidence.
    unfiltered = adapter.execute_tool("search_inventory", {"price_max": 40000})
    assert unfiltered["count"] > result["count"], (
        "expected the unfiltered live query to return more cars than the "
        "Toyota-filtered query, to prove the make filter is doing real work"
    )
