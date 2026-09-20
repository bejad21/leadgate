"""Prove the hold rules on the live Odoo, through the same XML-RPC calls the engine makes.

Uses throw-away catalog items that it creates and deletes, and lowers the global cap
for the duration of the run, so nothing real is touched. Run from the repo root:

    python n8n/scripts/verify_holds.py
"""
import os
import sys
import threading
import time
import xmlrpc.client

import httpx
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["ODOO_URL"].rstrip("/")
DB, USER, PASSWORD = os.environ["ODOO_DB"], os.environ["ODOO_USER"], os.environ["ODOO_PASSWORD"]
uid = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common").authenticate(DB, USER, PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")
PASSED = FAILED = 0


def call(model, method, args, kwargs=None):
    return models.execute_kw(DB, uid, PASSWORD, model, method, args, kwargs or {})


def concurrent_holds(item_ids, holders):
    """Fire one hold per item at the same moment, each from its own connection, the way
    simultaneous customers would. Returns every result."""
    results = [None] * len(item_ids)
    gate = threading.Barrier(len(item_ids))

    def worker(index):
        proxy = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")  # a proxy is not shared across threads
        gate.wait()
        results[index] = proxy.execute_kw(DB, uid, PASSWORD, "leadgate.catalog.item", "action_reserve", [[item_ids[index]]], {"holder": holders[index]})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(item_ids))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def run_expiry_job():
    """Run the real scheduled job, the way Odoo's scheduler would."""
    cron_id = call("ir.cron", "search", [[["name", "=", "LeadGate: release expired holds"]]])[0]
    call("ir.cron", "method_direct_trigger", [[cron_id]])


def remove_mirrored_probes():
    """The sync workflow mirrors every status change into Supabase, and a deleted Odoo
    record is not removed there, so the probes would linger as phantom keys."""
    base, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not base or not key:
        return
    time.sleep(4)  # let the last webhook land first
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    url = f"{base.rstrip('/')}/rest/v1/catalog_items?name=like.HOLD-PROBE*"
    httpx.delete(url, headers=headers, timeout=15)
    remaining = httpx.get(url + "&select=odoo_id", headers=headers, timeout=15).json()
    check("no probe items linger on the dashboard", remaining == [], str(remaining))


def check(name, ok, detail=""):
    global PASSED, FAILED
    ok = bool(ok)
    PASSED += ok
    FAILED += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main() -> int:
    ids = [call("leadgate.catalog.item", "create", [{"name": f"HOLD-PROBE {n}", "domain_type": "cars", "price": 1000 + n}]) for n in range(4)]
    original_cap = call("ir.config_parameter", "get_param", ["leadgate.max_active_holds"]) or ""
    try:
        call("ir.config_parameter", "set_param", ["leadgate.max_active_holds", "2"])
        a, b, c, d = ids

        r = call("leadgate.catalog.item", "action_reserve", [[a]], {"hours": 24, "lead_id": 777, "holder": "alice@example.com"})
        check("an available item can be held", r.get("ok") is True and r.get("reserved_until"), str(r))
        row = call("leadgate.catalog.item", "read", [[a], ["status", "reserved_for", "reservation_lead_id"]])[0]
        check("the item is now reserved for that customer and lead", row["status"] == "reserved" and row["reserved_for"] == "alice@example.com" and row["reservation_lead_id"] == 777, str(row))

        r = call("leadgate.catalog.item", "action_reserve", [[a]], {"holder": "bob@example.com"})
        check("a held item cannot be held again", r == {"ok": False, "reason": "not_available"}, str(r))

        r = call("leadgate.catalog.item", "action_reserve", [[b]], {"holder": "alice@example.com"})
        check("one customer cannot hold two items", r == {"ok": False, "reason": "holder_already_has_a_hold"}, str(r))

        r = call("leadgate.catalog.item", "action_reserve", [[b]], {"holder": "bob@example.com"})
        check("a second customer can hold a different item", r.get("ok") is True, str(r))

        r = call("leadgate.catalog.item", "action_reserve", [[c]], {"holder": "carol@example.com"})
        check("the global cap stops further holds", r == {"ok": False, "reason": "hold_limit_reached"}, str(r))

        call("leadgate.catalog.item", "write", [[a], {"status": "sold"}])
        row = call("leadgate.catalog.item", "read", [[a], ["status", "reserved_until", "reserved_for", "reservation_lead_id"]])[0]
        check("marking a held item sold clears the hold data", row["status"] == "sold" and not row["reserved_until"] and not row["reserved_for"] and row["reservation_lead_id"] == 0, str(row))

        call("leadgate.catalog.item", "write", [[b], {"reserved_until": "2000-01-01 00:00:00"}])
        run_expiry_job()
        row = call("leadgate.catalog.item", "read", [[b], ["status", "reserved_until"]])[0]
        check("the expiry job releases a lapsed hold", row["status"] == "available" and not row["reserved_until"], str(row))

        r = call("leadgate.catalog.item", "action_reserve", [[c]], {"holder": "carol@example.com"})
        check("a released slot can be used again", r.get("ok") is True, str(r))

        call("leadgate.catalog.item", "write", [[d], {"status": "reserved"}])
        run_expiry_job()
        row = call("leadgate.catalog.item", "read", [[d], ["status"]])[0]
        check("a status set by hand with no expiry is left alone", row["status"] == "reserved", str(row))

        # ---- simultaneous customers ------------------------------------------------
        call("leadgate.catalog.item", "action_release", [[c, d]])
        call("ir.config_parameter", "set_param", ["leadgate.max_active_holds", "1"])
        racers = [call("leadgate.catalog.item", "create", [{"name": f"HOLD-PROBE race {n}", "domain_type": "cars", "price": 2000 + n}]) for n in range(6)]
        ids.extend(racers)
        outcomes = concurrent_holds(racers, [f"racer{n}@example.com" for n in range(6)])
        granted = [o for o in outcomes if o.get("ok")]
        check("six simultaneous customers, a cap of one: exactly one hold is granted", len(granted) == 1, str(outcomes))

        call("leadgate.catalog.item", "action_release", [racers])
        call("ir.config_parameter", "set_param", ["leadgate.max_active_holds", "10"])
        same = [call("leadgate.catalog.item", "create", [{"name": f"HOLD-PROBE same {n}", "domain_type": "cars", "price": 3000 + n}]) for n in range(4)]
        ids.extend(same)
        outcomes = concurrent_holds(same, ["one.person@example.com"] * 4)
        granted = [o for o in outcomes if o.get("ok")]
        check("four simultaneous requests from one customer: exactly one hold is granted", len(granted) == 1, str(outcomes))

        cron = call("ir.cron", "search_read", [[["name", "=", "LeadGate: release expired holds"]]], {"fields": ["active", "interval_number", "interval_type"]})
        check("the expiry job is scheduled", bool(cron) and cron[0]["active"], str(cron))
    finally:
        call("leadgate.catalog.item", "unlink", [ids])
        call("ir.config_parameter", "set_param", ["leadgate.max_active_holds", original_cap or "10"])
        left = call("leadgate.catalog.item", "search_count", [[["name", "like", "HOLD-PROBE"]]])
        check("the probe items were removed", left == 0, str(left))
        remove_mirrored_probes()
    print(f"\n{PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
