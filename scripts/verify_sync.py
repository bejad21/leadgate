"""End-to-end check of Odoo -> engine -> Supabase: change a catalog item's status in Odoo and confirm
the matching `catalog_items` row follows.

    python scripts/verify_sync.py

Flips one `cars` item between available and sold over XML-RPC (that fires the "Catalog item status
changed" automated action, which calls the engine's POST /webhook/odoo-status), waits for Supabase's row
to show the new status and Odoo's write time, then puts the item back and waits for that too. Needs Odoo
and the engine running, Odoo pointed at the engine (scripts/set_odoo_webhook_param.py), and the item
already in Supabase (scripts/backfill_supabase.py). Prints only item ids and statuses.
"""
import os
import sys
import time
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from engine import supabase_rest  # noqa: E402

WAIT_SECONDS = 20


def odoo_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def supabase_row(odoo_id: int) -> dict | None:
    base, key = supabase_rest.base_url(), os.environ["SUPABASE_SERVICE_KEY"]
    response = httpx.get(
        f"{base}/rest/v1/catalog_items",
        params={"odoo_id": f"eq.{odoo_id}", "select": "odoo_id,status,updated_at"},
        headers=supabase_rest.service_headers(key),
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def wait_for(odoo_id: int, status: str, written_at: datetime) -> dict | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        row = supabase_row(odoo_id)
        if row and row["status"] == status and datetime.fromisoformat(row["updated_at"]) == written_at:
            return row
        time.sleep(0.5)
    return supabase_row(odoo_id)


def main() -> int:
    url, db = os.environ.get("ODOO_URL", "http://localhost:8069"), os.environ["ODOO_DB"]
    username, password = os.environ["ODOO_USER"], os.environ["ODOO_PASSWORD"]
    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, username, password, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(method, *args, **kwargs):
        return models.execute_kw(db, uid, password, "leadgate.catalog.item", method, list(args), kwargs)

    found = call("search_read", [["domain_type", "=", "cars"], ["status", "in", ["available", "sold"]]], fields=["id", "status"], limit=1)
    if not found:
        print("BLOCKED: no available or sold cars item in Odoo; seed it first", file=sys.stderr)
        return 2
    item_id, original = found[0]["id"], found[0]["status"]
    flipped = "sold" if original == "available" else "available"
    if supabase_row(item_id) is None:
        print(f"BLOCKED: item {item_id} is not in Supabase yet; run scripts/backfill_supabase.py", file=sys.stderr)
        return 2

    failures = 0
    for target in (flipped, original):
        call("write", [item_id], {"status": target})
        written_at = odoo_time(call("read", [item_id], fields=["write_date"])[0]["write_date"])
        row = wait_for(item_id, target, written_at)
        ok = bool(row) and row["status"] == target and datetime.fromisoformat(row["updated_at"]) == written_at
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} item {item_id} -> {target}: Supabase shows {row['status'] if row else 'no row'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
