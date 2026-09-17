from unittest.mock import MagicMock, patch

from engine.odoo_client import OdooClient


@patch("engine.odoo_client.xmlrpc.client.ServerProxy")
def test_search_read_returns_list_of_dicts(mock_server_proxy):
    mock_common = MagicMock()
    mock_common.authenticate.return_value = 7
    mock_models = MagicMock()
    mock_models.execute_kw.return_value = [{"id": 1, "name": "Toyota Corolla"}]
    mock_server_proxy.side_effect = [mock_common, mock_models]

    client = OdooClient(
        url="http://localhost:8069",
        db="leadgate",
        username="admin",
        password="admin",
    )

    result = client.search_read(
        "leadgate.catalog.item",
        [["make", "=", "Toyota"]],
        ["id", "name"],
    )

    assert result == [{"id": 1, "name": "Toyota Corolla"}]
    mock_models.execute_kw.assert_called_once_with(
        "leadgate",
        7,
        "admin",
        "leadgate.catalog.item",
        "search_read",
        [[["make", "=", "Toyota"]]],
        {"fields": ["id", "name"]},
    )


@patch("engine.odoo_client.xmlrpc.client.ServerProxy")
def test_create_returns_new_record_id(mock_server_proxy):
    mock_common = MagicMock()
    mock_common.authenticate.return_value = 7
    mock_models = MagicMock()
    mock_models.execute_kw.return_value = 42
    mock_server_proxy.side_effect = [mock_common, mock_models]

    client = OdooClient(
        url="http://localhost:8069",
        db="leadgate",
        username="admin",
        password="admin",
    )

    new_id = client.create("leadgate.catalog.item", {"name": "Honda Civic"})

    assert new_id == 42
    mock_models.execute_kw.assert_called_once_with(
        "leadgate",
        7,
        "admin",
        "leadgate.catalog.item",
        "create",
        [{"name": "Honda Civic"}],
    )
