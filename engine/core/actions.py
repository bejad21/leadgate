"""What the assistant may do for a customer beyond searching: hold an item, book a viewing.

Both are requests that a person confirms, not commitments. A hold lapses on its own, a
viewing is a dated task on a lead, and every rule that protects the catalog (one hold per
customer, a global cap, an item can only be held once) is enforced in Odoo or here in code,
never left to the model. Domain-agnostic, like the rest of engine/core.

The item comes from the catalog by id, and so does the price: what the model or the customer
says about either is not trusted.
"""
import datetime as dt
import logging
import re

from engine.core import followup
from engine.core.adapter_base import create_verified_lead
from engine.core.crm_contacts import parse_contact

logger = logging.getLogger(__name__)

HOLD_HOURS = 24
VIEWING_SLOTS = ("morning", "afternoon", "evening")
VIEWING_WINDOW_DAYS = 60

_ITEM_FIELDS = ["name", "price", "status", "domain_type", "reserved_for", "reservation_lead_id"]

_HOLD_REFUSALS = {
    "not_available": "That item is no longer available.",
    "hold_limit_reached": "Holds are not available right now. Tell the customer a person will follow up.",
    "holder_already_has_a_hold": "This customer already has an item on hold. Tell them a person will be in touch about it.",
}


def _error(message: str) -> dict:
    return {"error": message}


def holder_key(contact: str | None) -> str | None:
    """A stable identity for the person asking: their email if they gave one, else their digits."""
    email, phone = parse_contact(contact)
    if email:
        return email.lower()
    if phone:
        return re.sub(r"\D", "", phone)
    return None


def _load_item(odoo, domain_type: str, item_id) -> dict | None:
    if isinstance(item_id, bool) or not isinstance(item_id, (int, float)):
        return None
    rows = odoo.search_read(
        "leadgate.catalog.item",
        [("id", "=", int(item_id)), ("domain_type", "=", domain_type)],
        _ITEM_FIELDS,
    )
    return rows[0] if rows else None


def _no_such_item() -> dict:
    return _error("That item was not found. Search again and use an item's id from the results.")


def _needs_contact() -> dict:
    return _error("A phone number or email is required. Ask the customer for one.")


def _lead_args(item: dict, name: str, args: dict) -> dict:
    return {
        "name": name,
        "customer_name": args.get("customer_name"),
        "customer_contact": args.get("customer_contact"),
        "price": item["price"],  # the catalog's price, never the model's
        "notes": args.get("notes"),
    }


def reserve_item(odoo, domain_type: str, args: dict) -> dict:
    """Hold one available item for HOLD_HOURS and open a lead for a person to confirm it."""
    item = _load_item(odoo, domain_type, args.get("item_id"))
    if item is None:
        return _no_such_item()
    holder = holder_key(args.get("customer_contact"))
    if holder is None:
        return _needs_contact()

    if item["status"] == "reserved" and item.get("reserved_for") == holder and item.get("reservation_lead_id"):
        # The same customer asking again (a resend, or the model calling twice): same hold.
        return _reservation_result(item, item["reservation_lead_id"], duplicate=True)
    if item["status"] != "available":
        return _error(_HOLD_REFUSALS["not_available"])

    hold = odoo.call(
        "leadgate.catalog.item", "action_reserve", [[int(args["item_id"])]],
        {"hours": HOLD_HOURS, "lead_id": 0, "holder": holder},
    )
    if not hold.get("ok"):
        return _error(_HOLD_REFUSALS.get(hold.get("reason"), _HOLD_REFUSALS["not_available"]))

    try:
        lead = create_verified_lead(
            odoo,
            domain_type,
            _lead_args(item, f"Hold: {item['name']}", args),
            kind="reservation",
            extra_notes=(f"Reservation request: held for {HOLD_HOURS} hours. Confirm the sale or release it.",),
            follow_up={"summary": f"Confirm the hold on {item['name']} with {args.get('customer_name') or 'the customer'}", "days": 0},
        )
    except Exception:
        logger.exception("could not create the lead for a hold on item %s; giving the hold back", args.get("item_id"))
        try:
            odoo.call("leadgate.catalog.item", "action_release", [[int(args["item_id"])]], {})
        except Exception:
            logger.exception("could not release the hold on item %s", args.get("item_id"))
        return _error("The reservation could not be recorded. Tell the customer a person will follow up.")

    # Link the hold to its lead so the owner's buttons can find it. The hold and the lead already
    # exist, so a failure here is retried once and otherwise only logged: the customer still gets their
    # answer and the owner still gets the alert.
    for attempt in range(2):
        try:
            odoo.write("leadgate.catalog.item", [int(args["item_id"])], {"reservation_lead_id": lead["lead_id"]})
            break
        except Exception:
            logger.exception("could not link the hold on item %s to lead %s (attempt %s)", args.get("item_id"), lead["lead_id"], attempt + 1)
    return _reservation_result(item, lead["lead_id"], duplicate=lead.get("duplicate", False))


def _reservation_result(item: dict, lead_id: int, *, duplicate: bool = False) -> dict:
    result = {
        "lead_id": lead_id,
        "kind": "reservation",
        "item_name": item["name"],
        "price": item["price"],
        "detail": f"Held for {HOLD_HOURS} hours",
        "note": (
            f"The item is held for {HOLD_HOURS} hours while a person confirms. "
            "Do not tell the customer it is theirs yet, and do not promise a price or a time."
        ),
    }
    if duplicate:
        result["duplicate"] = True
    return result


def _parse_viewing_date(value, today: dt.date) -> dt.date | None:
    try:
        day = dt.date.fromisoformat(str(value))
    except ValueError:
        return None
    if day < today or day > today + dt.timedelta(days=VIEWING_WINDOW_DAYS):
        return None
    return day


def _describe_day(day: dt.date, slot: str) -> str:
    return f"{day:%a} {day.day} {day:%b}, {slot}"


def book_viewing(odoo, domain_type: str, args: dict, *, today: dt.date | None = None) -> dict:
    """Open a lead with a meeting on the requested day for a person to confirm."""
    today = today or dt.date.today()
    item = _load_item(odoo, domain_type, args.get("item_id"))
    if item is None:
        return _no_such_item()
    if item["status"] == "sold":
        return _error("That item is no longer available.")
    if holder_key(args.get("customer_contact")) is None:
        return _needs_contact()
    slot = args.get("slot")
    if slot not in VIEWING_SLOTS:
        return _error("Ask the customer whether they prefer the morning, afternoon or evening.")
    day = _parse_viewing_date(args.get("date"), today)
    if day is None:
        last = today + dt.timedelta(days=VIEWING_WINDOW_DAYS)
        return _error(f"The date must be between {today.isoformat()} and {last.isoformat()}. Ask the customer for a day in that range.")

    detail = _describe_day(day, slot)
    lead = create_verified_lead(
        odoo,
        domain_type,
        _lead_args(item, f"Viewing: {item['name']}", args),
        kind="viewing",
        extra_notes=(f"Requested viewing: {day.isoformat()} ({slot}). Confirm the time with the customer.",),
        follow_up={"summary": f"Viewing of {item['name']}: {slot}", "kind": "meeting", "due": day},
    )
    result = {
        "lead_id": lead["lead_id"],
        "kind": "viewing",
        "item_name": item["name"],
        "price": item["price"],
        "detail": detail,
        "note": "The viewing is requested, not confirmed. A person will confirm the exact time. Do not promise it.",
    }
    if lead.get("duplicate"):
        result["duplicate"] = True
    return result


def action_tool_schemas(noun: str) -> list[dict]:
    """Function-calling schemas for the two actions. `noun` is what the catalog calls an item."""
    return [
        {
            "type": "function",
            "function": {
                "name": "reserve_item",
                "description": (
                    f"Hold one {noun} for {HOLD_HOURS} hours for a customer who says they want it, and open a "
                    "lead for a person to confirm. Use the id from the search results. Needs the "
                    "customer's name and a phone number or email."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": f"The {noun}'s id from the search results"},
                        "customer_name": {"type": "string"},
                        "customer_contact": {"type": "string", "description": "Customer's email or phone number"},
                        "notes": {"type": "string", "description": "Anything the customer said that a person should know"},
                    },
                    "required": ["item_id", "customer_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_viewing",
                "description": (
                    f"Request a viewing of one {noun} on a day the customer chose. A person confirms the exact "
                    "time. Use the id from the search results. Needs the customer's name, a phone number or "
                    "email, a date and a time of day."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": f"The {noun}'s id from the search results"},
                        "customer_name": {"type": "string"},
                        "customer_contact": {"type": "string", "description": "Customer's email or phone number"},
                        "date": {"type": "string", "description": "The day, as YYYY-MM-DD, today or later"},
                        "slot": {"type": "string", "enum": list(VIEWING_SLOTS)},
                        "notes": {"type": "string", "description": "Anything the customer said that a person should know"},
                    },
                    "required": ["item_id", "customer_name", "date", "slot"],
                },
            },
        },
    ]


ACTION_TOOLS = ("reserve_item", "book_viewing")


def execute_action_tool(odoo, domain_type: str, name: str, args: dict) -> dict:
    if name == "reserve_item":
        return reserve_item(odoo, domain_type, args)
    if name == "book_viewing":
        return book_viewing(odoo, domain_type, args)
    raise ValueError(f"Unknown tool: {name}")
