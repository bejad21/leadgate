"""The engine must not run with Odoo's default password, and the module defines a limited account for it."""
import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import engine.config as config_module
from engine.config import load_config
from engine.tests.test_config import BASE_ENV

MODULE = Path(__file__).resolve().parents[2] / "odoo" / "addons" / "leadgate_domain"


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ODOO_PASSWORD", "a-long-private-password")
    monkeypatch.delenv("LEADGATE_ALLOW_DEFAULT_ODOO_PASSWORD", raising=False)


@pytest.mark.parametrize("password", ["admin", "Admin", " admin ", "odoo", "password"])
def test_a_default_odoo_password_stops_the_engine(monkeypatch, password):
    monkeypatch.setenv("ODOO_PASSWORD", password)
    with pytest.raises(RuntimeError, match="ODOO_PASSWORD"):
        load_config()


def test_a_local_test_setup_can_allow_it_on_purpose(monkeypatch):
    monkeypatch.setenv("ODOO_PASSWORD", "admin")
    monkeypatch.setenv("LEADGATE_ALLOW_DEFAULT_ODOO_PASSWORD", "1")
    assert load_config()["ODOO_PASSWORD"] == "admin"


def test_the_admin_user_is_pointed_out_in_the_log(monkeypatch, caplog):
    monkeypatch.setenv("ODOO_USER", "admin")
    load_config()
    assert any("admin" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_a_named_engine_account_starts_quietly(monkeypatch, caplog):
    monkeypatch.setenv("ODOO_USER", "leadgate-engine")
    load_config()
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "ODOO_USER" in r.getMessage()]


def access_rows():
    with open(MODULE / "security" / "ir.model.access.csv", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_the_engine_group_may_read_and_change_items_but_not_create_or_delete_them():
    rows = [r for r in access_rows() if r["group_id:id"] == "group_engine" and r["model_id:id"] == "model_leadgate_catalog_item"]
    assert [(r["perm_read"], r["perm_write"], r["perm_create"], r["perm_unlink"]) for r in rows] == [("1", "1", "0", "0")]


def test_only_administrators_create_or_delete_catalog_items():
    for row in access_rows():
        if row["model_id:id"] == "model_leadgate_catalog_item" and (row["perm_create"], row["perm_unlink"]) != ("0", "0"):
            assert row["group_id:id"] == "base.group_system", row


def test_the_group_is_declared_and_loaded_before_the_access_rules():
    group = ET.parse(MODULE / "security" / "engine_group.xml").getroot().find(".//record[@id='group_engine']")
    assert group is not None and group.get("model") == "res.groups"
    manifest = (MODULE / "__manifest__.py").read_text(encoding="utf-8")
    assert manifest.index("security/engine_group.xml") < manifest.index("security/ir.model.access.csv")
