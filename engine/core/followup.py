"""What happens to a lead once it exists: who owns it, what is due, what stage it is in.

Domain-agnostic, like the rest of engine/core: it only knows about CRM leads. Everything
here is a small, separate Odoo call, and callers treat a failure as non-fatal: the lead
is already in the CRM, and a missing reminder must never cost the customer their lead.
"""
import datetime as dt
import os

ACTIVITY_TYPES = {
    "call": "mail.mail_activity_data_call",
    "meeting": "mail.mail_activity_data_meeting",
    "todo": "mail.mail_activity_data_todo",
}

# Looked up once per process; these ids do not change under us.
_user_ids: dict[str, int | None] = {}
_xmlid_ids: dict[str, int | None] = {}
_model_ids: dict[str, int] = {}


def salesperson_login() -> str | None:
    return os.environ.get("ODOO_SALESPERSON_LOGIN") or os.environ.get("ODOO_USER")


def salesperson_id(odoo) -> int | None:
    """The Odoo user that new leads are assigned to, or None if that login does not exist."""
    login = salesperson_login()
    if not login:
        return None
    if login not in _user_ids:
        rows = odoo.search_read("res.users", [("login", "=", login)], ["id"], limit=1)
        _user_ids[login] = rows[0]["id"] if rows else None
    return _user_ids[login]


def _xmlid_id(odoo, xmlid: str) -> int | None:
    if xmlid not in _xmlid_ids:
        module, name = xmlid.split(".", 1)
        rows = odoo.search_read(
            "ir.model.data", [("module", "=", module), ("name", "=", name)], ["res_id"], limit=1
        )
        _xmlid_ids[xmlid] = rows[0]["res_id"] if rows else None
    return _xmlid_ids[xmlid]


def _model_id(odoo, model: str) -> int:
    if model not in _model_ids:
        _model_ids[model] = odoo.search_read("ir.model", [("model", "=", model)], ["id"], limit=1)[0]["id"]
    return _model_ids[model]


def schedule_activity(
    odoo,
    lead_id: int,
    *,
    summary: str,
    kind: str = "call",
    days: int = 1,
    due: dt.date | None = None,
    today: dt.date | None = None,
) -> None:
    """Put a dated task on the lead for the salesperson (a call by default).

    Created directly rather than through crm.lead.activity_schedule: that method does
    its work but returns a value Odoo's XML-RPC cannot marshal, which surfaces as an
    error on every lead even though the task was made.
    """
    deadline = due or ((today or dt.date.today()) + dt.timedelta(days=days))
    values = {
        "res_model_id": _model_id(odoo, "crm.lead"),
        "res_id": lead_id,
        "activity_type_id": _xmlid_id(odoo, ACTIVITY_TYPES[kind]),
        "date_deadline": deadline.isoformat(),
        "summary": summary,
    }
    user_id = salesperson_id(odoo)
    if user_id:
        values["user_id"] = user_id
    odoo.create("mail.activity", values)


def set_stage(odoo, lead_id: int, stage_name: str) -> bool:
    rows = odoo.search_read("crm.stage", [("name", "=", stage_name)], ["id"], limit=1)
    if not rows:
        return False
    odoo.write("crm.lead", [lead_id], {"stage_id": rows[0]["id"]})
    return True


def mark_lost(odoo, lead_id: int) -> None:
    """Archive the lead with zero probability, which is what Odoo shows as Lost.
    (crm.lead.action_set_lost does the same but cannot be marshalled over XML-RPC.)"""
    odoo.write("crm.lead", [lead_id], {"active": False, "probability": 0})


def complete_activities(odoo, lead_id: int, feedback: str) -> None:
    """Close the lead's open tasks, recording what was done in the chatter."""
    rows = odoo.search_read(
        "mail.activity", [("res_model", "=", "crm.lead"), ("res_id", "=", lead_id)], ["id"]
    )
    if rows:
        odoo.call("mail.activity", "action_feedback", [[r["id"] for r in rows]], {"feedback": feedback})


def post_note(odoo, lead_id: int, text: str) -> None:
    """Add an internal note to the lead. The text may come from a customer. It is passed as
    a plain string, which Odoo escapes itself; escaping it here as well would double-escape."""
    odoo.call(
        "crm.lead",
        "message_post",
        [[lead_id]],
        {"body": text, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
    )
