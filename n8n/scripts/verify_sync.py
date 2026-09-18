"""End-to-end verification for Task 3.2/3.3.

Triggers a real status change on one `cars` and one `real_estate`
leadgate.catalog.item via XML-RPC (which fires the existing
"Catalog item status changed" Automated Action -> _notify_webhook -> the
n8n webhook), waits briefly for the workflow to run, then independently
queries Supabase (via its REST API) and MongoDB (via pymongo) to confirm
the data actually landed -- not just that n8n reported success.
"""
import os
import time
import xmlrpc.client

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def odoo_call(models, db, uid, password, model, method, *args, **kwargs):
    return models.execute_kw(db, uid, password, model, method, list(args), kwargs)


def trigger_status_changes():
    url = os.environ.get("ODOO_URL", "http://localhost:8069")
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USER"]
    password = os.environ["ODOO_PASSWORD"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, password, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    changed = []
    for domain_type in ["cars", "real_estate"]:
        rec = odoo_call(
            models, db, uid, password, "leadgate.catalog.item", "search_read",
            [["domain_type", "=", domain_type]], fields=["id", "status"], limit=1,
        )
        if not rec:
            continue
        rid = rec[0]["id"]
        new_status = "sold" if rec[0]["status"] != "sold" else "available"
        odoo_call(models, db, uid, password, "leadgate.catalog.item", "write", [rid], {"status": new_status})
        changed.append((rid, domain_type, new_status))
        print(f"Triggered: {domain_type} record {rid} -> status={new_status}")
    return changed


def check_supabase(odoo_ids):
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_KEY"]
    ids_filter = ",".join(str(i) for i in odoo_ids)
    r = httpx.get(
        f"{supabase_url}/rest/v1/catalog_items",
        headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
        params={"odoo_id": f"in.({ids_filter})", "select": "*"},
        timeout=15,
    )
    print("Supabase catalog_items query:", r.status_code)
    print(r.text)


def check_mongo(odoo_ids):
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10000)
    db = client["leadgate"]
    for doc in db["catalog_events"].find({"odoo_id": {"$in": odoo_ids}}):
        print("MongoDB catalog_events document:", doc)


def main():
    changed = trigger_status_changes()
    if not changed:
        print("No records found to change -- nothing to verify.")
        return
    print("Waiting 3s for the n8n workflow to finish processing...")
    time.sleep(3)
    ids = [rid for rid, _, _ in changed]
    check_supabase(ids)
    check_mongo(ids)


if __name__ == "__main__":
    main()
