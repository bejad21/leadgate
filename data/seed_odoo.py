"""Seed leadgate.catalog.item from data/cars_raw.csv and data/real_estate_raw.csv.

Usage:
    .venv/Scripts/python.exe data/seed_odoo.py

Reads ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD from .env (or the
environment), logs into Odoo via XML-RPC, and bulk-creates one
leadgate.catalog.item record per CSV row:
  - cars_raw.csv rows      -> domain_type="cars"
  - real_estate_raw.csv rows -> domain_type="real_estate"

Every column other than `price` is packed into the `attributes` field as a
JSON string.
"""
import csv
import json
import os
import xmlrpc.client
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
CARS_CSV = DATA_DIR / "cars_raw.csv"
REAL_ESTATE_CSV = DATA_DIR / "real_estate_raw.csv"


def load_env(env_path: Path) -> None:
    """Minimal .env loader (no external dependency)."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        os.environ.setdefault(key, value)


load_env(DATA_DIR.parent / ".env")

ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "leadgate")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")


def read_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cars_to_record(row: dict) -> dict:
    make = row.get("make", "").strip()
    model = row.get("model", "").strip()
    year = row.get("year", "").strip()
    price = float(row["price"])
    attributes = {k: v for k, v in row.items() if k != "price"}
    return {
        "name": f"{year} {make} {model}".strip(),
        "domain_type": "cars",
        "attributes": json.dumps(attributes),
        "price": price,
        "status": "available",
    }


def real_estate_to_record(row: dict) -> dict:
    bed = row.get("bed", "").strip()
    bath = row.get("bath", "").strip()
    property_type = row.get("property_type", "").strip()
    city = row.get("city", "").strip()
    state = row.get("state", "").strip()
    price = float(row["price"])
    attributes = {k: v for k, v in row.items() if k != "price"}
    return {
        "name": f"{bed}bd/{bath}ba {property_type} - {city}, {state}".strip(),
        "domain_type": "real_estate",
        "attributes": json.dumps(attributes),
        "price": price,
        "status": "available",
    }


def main() -> None:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise SystemExit("Odoo authentication failed - check ODOO_DB/ODOO_USER/ODOO_PASSWORD")

    models_proxy = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    def create_many(records: list[dict]) -> list[int]:
        return models_proxy.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "leadgate.catalog.item", "create", [records]
        )

    cars_rows = read_rows(CARS_CSV)
    real_estate_rows = read_rows(REAL_ESTATE_CSV)

    cars_records = [cars_to_record(r) for r in cars_rows]
    real_estate_records = [real_estate_to_record(r) for r in real_estate_rows]

    cars_ids = create_many(cars_records)
    real_estate_ids = create_many(real_estate_records)

    print(f"Created {len(cars_ids)} cars records (ids {cars_ids[0]}..{cars_ids[-1]})")
    print(
        f"Created {len(real_estate_ids)} real_estate records "
        f"(ids {real_estate_ids[0]}..{real_estate_ids[-1]})"
    )
    print(f"Total created this run: {len(cars_ids) + len(real_estate_ids)}")


if __name__ == "__main__":
    main()
