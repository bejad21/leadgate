"""Copy every catalog item from Odoo into Supabase's catalog_items table.

The n8n workflow only mirrors an item when its status changes, so a fresh
Supabase table starts empty and the dashboard would show only items that have
changed. Run this once after seeding Odoo (and again any time you want to
resync). It is idempotent: rows are upserted on odoo_id, so running it twice
changes nothing.

Usage (from the repo root, with the virtualenv active):
    python n8n/scripts/backfill_supabase.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from engine.odoo_client import OdooClient  # noqa: E402

BATCH_SIZE = 200


def odoo_timestamp(value: str) -> str:
    """Odoo stores UTC as 'YYYY-MM-DD HH:MM:SS'; Supabase wants ISO 8601."""
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def main() -> None:
    odoo = OdooClient(
        os.environ["ODOO_URL"], os.environ["ODOO_DB"], os.environ["ODOO_USER"], os.environ["ODOO_PASSWORD"]
    )
    records = odoo.search_read(
        "leadgate.catalog.item",
        [],
        ["name", "domain_type", "price", "status", "write_date"],
    )
    rows = [
        {
            "odoo_id": r["id"],
            "domain_type": r["domain_type"],
            "name": r["name"],
            "price": r["price"],
            "status": r["status"],
            "updated_at": odoo_timestamp(r["write_date"]),
        }
        for r in records
    ]

    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/catalog_items?on_conflict=odoo_id"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        response = httpx.post(url, headers=headers, json=batch, timeout=60)
        response.raise_for_status()
        print(f"upserted {start + len(batch)}/{len(rows)}")

    print(f"Done: {len(rows)} catalog items mirrored to Supabase.")


if __name__ == "__main__":
    main()
