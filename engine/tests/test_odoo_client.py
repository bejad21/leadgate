import socket
import time
import xmlrpc.client
from unittest.mock import MagicMock, patch

import pytest

from engine.odoo_client import DEFAULT_TIMEOUT_SECONDS, OdooClient, OdooLoginError, make_transport


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


def test_failed_login_raises_without_leaking_the_password():
    class Common:
        def authenticate(self, *args):
            return False

    with patch("engine.odoo_client.xmlrpc.client.ServerProxy", return_value=Common()):
        with pytest.raises(OdooLoginError) as caught:
            OdooClient("http://odoo.test", "shop_db", "alice", "s3cret-pass")

    message = str(caught.value)
    assert "alice" in message and "shop_db" in message
    assert "s3cret-pass" not in message


@patch("engine.odoo_client.xmlrpc.client.ServerProxy")
def test_both_proxies_get_a_transport_that_carries_the_timeout(mock_server_proxy):
    mock_common = MagicMock()
    mock_common.authenticate.return_value = 7
    mock_server_proxy.side_effect = [mock_common, MagicMock()]

    client = OdooClient("http://odoo.test", "db", "u", "p", timeout=12.5)
    _ = client.models

    transports = [call.kwargs["transport"] for call in mock_server_proxy.call_args_list]
    assert len(transports) == 2
    assert [t.timeout for t in transports] == [12.5, 12.5]
    for transport in transports:
        connection = transport.make_connection("odoo.test:8069")
        assert connection.timeout == 12.5


def test_https_urls_use_the_safe_transport_with_the_timeout():
    transport = make_transport("https://odoo.test", 9)

    assert isinstance(transport, xmlrpc.client.SafeTransport)
    assert transport.make_connection("odoo.test").timeout == 9


def test_default_timeout_is_thirty_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 30


def test_a_server_that_never_answers_times_out_instead_of_hanging():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        started = time.monotonic()
        with pytest.raises(OSError):  # socket.timeout is a subclass of OSError
            OdooClient(f"http://127.0.0.1:{listener.getsockname()[1]}", "db", "u", "p", timeout=0.3)
        assert time.monotonic() - started < 5
    finally:
        listener.close()


@pytest.mark.parametrize("value, expected", [("7", 7.0), ("0.5", 0.5), (" 12 ", 12.0)])
def test_the_timeout_env_var_is_read_when_the_client_is_built(monkeypatch, value, expected):
    # Set after engine.odoo_client was imported, as load_dotenv() does for a value in .env.
    monkeypatch.setenv("ODOO_TIMEOUT_SECONDS", value)
    mock_common = MagicMock()
    mock_common.authenticate.return_value = 7

    with patch("engine.odoo_client.xmlrpc.client.ServerProxy", side_effect=[mock_common]) as proxy:
        client = OdooClient("http://odoo.test", "db", "u", "p")

    assert client.timeout == expected
    assert proxy.call_args.kwargs["transport"].timeout == expected


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_timeout_env_var_means_the_default(monkeypatch, value):
    monkeypatch.setenv("ODOO_TIMEOUT_SECONDS", value)
    mock_common = MagicMock()
    mock_common.authenticate.return_value = 7

    with patch("engine.odoo_client.xmlrpc.client.ServerProxy", side_effect=[mock_common]):
        assert OdooClient("http://odoo.test", "db", "u", "p").timeout == DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", ["abc", "0", "-5", "nan", "inf", "-inf"])
def test_a_bad_timeout_env_var_is_a_clear_error_naming_the_variable(monkeypatch, value):
    monkeypatch.setenv("ODOO_TIMEOUT_SECONDS", value)

    with patch("engine.odoo_client.xmlrpc.client.ServerProxy") as proxy:
        with pytest.raises(ValueError, match="ODOO_TIMEOUT_SECONDS"):
            OdooClient("http://odoo.test", "db", "u", "p")

    proxy.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, float("nan")])
def test_a_bad_timeout_argument_is_rejected_too(value):
    with pytest.raises(ValueError, match="timeout"):
        OdooClient("http://odoo.test", "db", "u", "p", timeout=value)
