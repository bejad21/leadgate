"""Prove the follow-up on a real Odoo: a new lead is assigned and gets a call, and the stage,
note, task and lost helpers do what they say. Uses one throw-away lead and removes it.

    python n8n/scripts/verify_followup.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))
from engine.core import followup
from engine.core.adapter_base import create_verified_lead
from engine.odoo_client import OdooClient

o = OdooClient(os.environ["ODOO_URL"], os.environ["ODOO_DB"], os.environ["ODOO_USER"], os.environ["ODOO_PASSWORD"])
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


res = create_verified_lead(o, "cars", {"name": "T2 Probe Corolla", "customer_name": "Probe Person", "customer_contact": "probe.t2@example.com +971 50 555 0101"})
lid = res["lead_id"]
try:
    lead = o.search_read("crm.lead", [("id", "=", lid)], ["user_id", "stage_id", "partner_id"])[0]
    check("the lead is assigned to the salesperson", lead["user_id"] and lead["user_id"][0] == followup.salesperson_id(o), str(lead["user_id"]))
    acts = o.search_read("mail.activity", [("res_model", "=", "crm.lead"), ("res_id", "=", lid)], ["summary", "date_deadline", "user_id", "activity_type_id"])
    check("exactly one follow-up task exists", len(acts) == 1, str(acts))
    check("it is a Call for the salesperson", acts and acts[0]["activity_type_id"][1] == "Call" and acts[0]["user_id"][0] == lead["user_id"][0], str(acts))
    check("it names the customer and the item", acts and "Probe Person" in acts[0]["summary"] and "T2 Probe Corolla" in acts[0]["summary"], str(acts and acts[0]["summary"]))

    check("moving to Qualified works", followup.set_stage(o, lid, "Qualified"))
    check("the stage changed", o.search_read("crm.lead", [("id", "=", lid)], ["stage_id"])[0]["stage_id"][1] == "Qualified")
    followup.post_note(o, lid, "<b>hello</b> from the owner")
    msgs = o.search_read("mail.message", [("model", "=", "crm.lead"), ("res_id", "=", lid), ("message_type", "=", "comment")], ["body"])
    check("a note appears in the chatter, escaped", msgs and "&lt;b&gt;" in msgs[0]["body"], str(msgs))
    followup.complete_activities(o, lid, "Called the customer")
    check("finishing the task closes it", o.search_read("mail.activity", [("res_model", "=", "crm.lead"), ("res_id", "=", lid)], ["id"]) == [])
    followup.mark_lost(o, lid)
    lost = o.models.execute_kw(o.db, o.uid, o.password, "crm.lead", "search_read", [[("id", "=", lid), ("active", "=", False)]], {"fields": ["probability", "active"]})
    check("marking it lost archives it", bool(lost), str(lost))
finally:
    o.models.execute_kw(o.db, o.uid, o.password, "crm.lead", "unlink", [[lid]])
    pid = o.search_read("res.partner", [("email", "=", "probe.t2@example.com")], ["id"])
    if pid:
        o.models.execute_kw(o.db, o.uid, o.password, "res.partner", "unlink", [[p["id"] for p in pid]])
    check("probe records removed", o.search_read("crm.lead", [("id", "=", lid)], ["id"]) == [])
print("ALL OK" if ok else "SOME FAILED")
