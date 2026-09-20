"""A small in-memory stand-in for the Odoo client, shared by the follow-up and action tests.

It answers the handful of lookups the engine makes and records what was created, written
and called, so a test can assert on Odoo's end state instead of on a mock's call list.
"""


class FakeOdoo:
    def __init__(self, users=None, stages=None, activities=None, items=None, leads=None):
        self.users = users if users is not None else {"admin": 2}
        self.stages = stages if stages is not None else {"New": 1, "Qualified": 2, "Won": 4}
        self.activities = activities if activities is not None else []
        # id -> catalog item row
        self.items = items if items is not None else {}
        self.calls = []
        self.created = []
        self.written = []
        self.leads = leads if leads is not None else []  # what a search of crm.lead returns
        self.holds_refused_with = None  # set to a reason to make action_reserve refuse

    # ---- reads ---------------------------------------------------------------
    def search_read(self, model, domain, fields, **kwargs):
        if model == "res.users":
            login = domain[0][2]
            return [{"id": self.users[login]}] if login in self.users else []
        if model == "crm.stage":
            name = domain[0][2]
            return [{"id": self.stages[name]}] if name in self.stages else []
        if model == "mail.activity":
            return list(self.activities)
        if model == "ir.model.data":
            return [{"res_id": 40 + len(domain[1][2])}]
        if model == "ir.model":
            return [{"id": 99}]
        if model == "leadgate.catalog.item":
            return [dict(row) for row in self.items.values() if _matches(row, domain)]
        if model == "crm.lead":
            rows = [dict(row) for row in self.leads]
            for clause in domain:
                if isinstance(clause, tuple) and clause[0] == "id" and clause[1] == "=":
                    rows = [row for row in rows if row["id"] == clause[2]]
            return rows
        return []

    # ---- writes --------------------------------------------------------------
    def create(self, model, values):
        self.created.append((model, values))
        return 500 + len(self.created)

    def write(self, model, ids, values):
        self.written.append((model, ids, values))
        if model == "leadgate.catalog.item":
            for item_id in ids:
                self.items[item_id].update(values)
        return True

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method, args, kwargs))
        if model == "leadgate.catalog.item" and method == "action_reserve":
            return self._reserve(args[0][0], kwargs)
        if model == "leadgate.catalog.item" and method == "action_release":
            for item_id in args[0]:
                self.items[item_id]["status"] = "available"
            return len(args[0])
        return True

    def _reserve(self, item_id, kwargs):
        item = self.items[item_id]
        if self.holds_refused_with:
            return {"ok": False, "reason": self.holds_refused_with}
        if item["status"] != "available":
            return {"ok": False, "reason": "not_available"}
        item.update(status="reserved", reserved_for=kwargs.get("holder"), reservation_lead_id=kwargs.get("lead_id", 0))
        return {"ok": True, "reserved_until": "2026-09-21 14:00:00"}


def _matches(row, domain):
    for clause in domain:
        if not isinstance(clause, tuple):
            continue
        field, op, value = clause
        actual = row.get(field)
        if op == "=" and actual != value:
            return False
        if op == "!=" and actual == value:
            return False
        if op == "in" and actual not in value:
            return False
    return True


def item(item_id, name="2024 Toyota Corolla", price=21950.0, status="available", domain_type="cars"):
    return {
        "id": item_id,
        "name": name,
        "price": price,
        "status": status,
        "domain_type": domain_type,
        "reserved_for": False,
        "reservation_lead_id": 0,
    }


def lead_row(lead_id, stage="New", active=True, **extra):
    """What Odoo returns for a lead: its stage, whether it is archived, and its contact."""
    return {"id": lead_id, "stage_id": [1, stage], "active": active, "probability": 10, "email_from": None, "phone": None, **extra}
