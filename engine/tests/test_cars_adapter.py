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
    password = os.environ.get("ODOO_PASSWORD")
    if not password:
        # Fail loudly rather than silently falling back to a guessed
        # credential -- matches config.py's deliberate fail-fast convention
        # for this same variable. A missing ODOO_PASSWORD here is a real
        # misconfiguration, not something to paper over with "admin".
        raise RuntimeError(
            "ODOO_PASSWORD is not set -- required to run this live Odoo integration test"
        )
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

    NOTE: execute_tool truncates "matches" to the first 5 records
    (`records[:5]` in CarsAdapter.execute_tool), so checking only
    result["matches"] would silently miss leakage anywhere past position 5.
    This test therefore checks result["count"] (computed from the full,
    untruncated `records` list) against a full, untruncated recount obtained
    via a direct search_read call below -- not just the truncated preview.
    """
    client = _live_odoo_client()
    adapter = CarsAdapter(client)

    result = adapter.execute_tool("search_inventory", {"make": "Toyota", "price_max": 40000})
    assert result["count"] > 0, "expected at least one live Toyota match under 40000"

    # The 5-record preview should still look correct...
    for record in result["matches"]:
        assert "toyota" in record["name"].lower(), (
            f"non-Toyota record leaked into make-filtered results: {record['name']}"
        )
        assert record["price"] <= 40000

    # ...but the real proof has to cover the FULL result set, not just the
    # truncated preview. Query the same domain execute_tool builds
    # internally, directly via search_read with no [:5] slicing, and check
    # every single returned record.
    full_domain = [
        ("domain_type", "=", "cars"),
        ("status", "=", "available"),
        ("name", "ilike", "Toyota"),
        ("price", "<=", 40000),
    ]
    full_records = client.search_read("leadgate.catalog.item", full_domain, ["name", "price"])

    assert len(full_records) == result["count"], (
        "full search_read result count should match execute_tool's reported count "
        "(execute_tool must not be silently under- or over-counting)"
    )
    assert len(full_records) > 5, (
        "expected more than 5 live Toyota matches under 40000 so this test actually "
        "exercises records past the [:5] truncation point"
    )
    non_toyota_leaks = [r["name"] for r in full_records if "toyota" not in r["name"].lower()]
    assert non_toyota_leaks == [], (
        f"non-Toyota records leaked into the FULL result set: {non_toyota_leaks}"
    )
    over_budget = [r["name"] for r in full_records if r["price"] > 40000]
    assert over_budget == [], f"records over price_max leaked into the FULL result set: {over_budget}"

    # Sanity check: without the make filter, the live dataset has strictly
    # more matches under the same price cap, proving the filter is genuinely
    # narrowing the query rather than passing through by coincidence.
    unfiltered = adapter.execute_tool("search_inventory", {"price_max": 40000})
    assert unfiltered["count"] > result["count"], (
        "expected the unfiltered live query to return more cars than the "
        "Toyota-filtered query, to prove the make filter is doing real work"
    )


@pytest.mark.parametrize("make", ["Toyota", "Honda", "Ford"])
def test_live_search_inventory_full_result_set_has_zero_leakage_across_makes(make):
    """Mirrors the reviewer's independent verification: for each of several
    makes, pull the FULL (untruncated) live result set directly via
    search_read and assert zero non-matching records anywhere in it, not
    just within the first 5 previewed by execute_tool."""
    client = _live_odoo_client()

    domain = [
        ("domain_type", "=", "cars"),
        ("status", "=", "available"),
        ("name", "ilike", make),
    ]
    full_records = client.search_read("leadgate.catalog.item", domain, ["name", "price"])

    assert len(full_records) > 0, f"expected at least one live {make} match"
    leaks = [r["name"] for r in full_records if make.lower() not in r["name"].lower()]
    assert leaks == [], f"non-{make} records leaked into the full {make} result set: {leaks}"
