# odoo/addons/leadgate_domain/models/catalog_item.py
from datetime import timedelta

from odoo import api, fields, models

MAX_ACTIVE_HOLDS_PARAM = "leadgate.max_active_holds"
DEFAULT_MAX_ACTIVE_HOLDS = 10
MAX_HOLD_HOURS = 72


class CatalogItem(models.Model):
    _name = "leadgate.catalog.item"
    _description = "Domain-agnostic catalog item (car or property)"

    name = fields.Char(required=True)
    domain_type = fields.Selection(
        [("cars", "Cars"), ("real_estate", "Real Estate")], required=True, index=True
    )
    attributes = fields.Text(help="JSON-encoded domain-specific fields")
    price = fields.Float(required=True)
    status = fields.Selection(
        [("available", "Available"), ("reserved", "Reserved"), ("sold", "Sold")],
        default="available",
        required=True,
    )
    reserved_until = fields.Datetime(
        help="A hold placed through the assistant lapses at this time and the item goes back on the board."
    )
    reserved_for = fields.Char(help="Normalised email or phone of the customer holding the item.")
    reservation_lead_id = fields.Integer(help="The CRM lead created with this hold.")

    # ------------------------------------------------------------------ holds
    @api.model
    def _max_active_holds(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(MAX_ACTIVE_HOLDS_PARAM)
        try:
            return max(0, int(raw)) if raw else DEFAULT_MAX_ACTIVE_HOLDS
        except ValueError:
            return DEFAULT_MAX_ACTIVE_HOLDS

    def action_reserve(self, hours=24, lead_id=0, holder=""):
        """Place a timed hold on one item. Returns {"ok": bool, "reason": str, ...}.

        Everything that decides whether the hold is allowed happens here, in one transaction
        under a global lock, so two customers asking at once cannot both get the same item
        and a flood of requests cannot hold the whole catalog.

        The checks read through a fresh connection, not through this transaction. Odoo
        transactions see a snapshot of the database taken at their first statement, so a hold
        committed by the request that held the lock just before us would be invisible to a
        count made inside this transaction, and both requests would pass the cap. A fresh
        connection opened after the lock is granted sees everything that request committed.
        """
        self.ensure_one()
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext('leadgate_reserve'))")
        hours = max(1, min(int(hours or 24), MAX_HOLD_HOURS))
        with self.env.registry.cursor() as fresh:
            fresh.execute("SELECT status FROM leadgate_catalog_item WHERE id = %s", (self.id,))
            row = fresh.fetchone()
            if not row or row[0] != "available":
                return {"ok": False, "reason": "not_available"}
            fresh.execute(
                "SELECT count(*) FROM leadgate_catalog_item WHERE status = 'reserved' AND reserved_until IS NOT NULL"
            )
            active_holds = fresh.fetchone()[0]
            held_by_holder = False
            if holder:
                fresh.execute(
                    "SELECT 1 FROM leadgate_catalog_item WHERE status = 'reserved' "
                    "AND reserved_until IS NOT NULL AND reserved_for = %s LIMIT 1",
                    (holder,),
                )
                held_by_holder = fresh.fetchone() is not None
        if active_holds >= self._max_active_holds():
            return {"ok": False, "reason": "hold_limit_reached"}
        if held_by_holder:
            return {"ok": False, "reason": "holder_already_has_a_hold"}
        until = fields.Datetime.now() + timedelta(hours=hours)
        self.write(
            {
                "status": "reserved",
                "reserved_until": until,
                "reserved_for": holder or False,
                "reservation_lead_id": lead_id or 0,
            }
        )
        return {"ok": True, "reserved_until": fields.Datetime.to_string(until)}

    def action_release(self):
        """Put a held item back on the board. Does nothing to an item that is not held."""
        released = 0
        for record in self:
            if record.status == "reserved":
                record.write({"status": "available"})
                released += 1
        return released

    @api.model
    def _cron_release_expired(self):
        expired = self.search(
            [("status", "=", "reserved"), ("reserved_until", "!=", False), ("reserved_until", "<", fields.Datetime.now())]
        )
        expired.action_release()
        return len(expired)

    def write(self, vals):
        # A hold is only meaningful while the item is reserved; if someone changes the status
        # by hand (sold, or back to available) the leftover hold data must not linger and
        # trip the expiry job later.
        if "status" in vals and vals["status"] != "reserved" and "reserved_until" not in vals:
            vals = dict(vals, reserved_until=False, reserved_for=False, reservation_lead_id=0)
        return super().write(vals)
