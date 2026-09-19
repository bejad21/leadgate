"""Turn what a customer typed into real Odoo contact data.

A lead used to carry the customer's name and email as a paragraph of text.
Here the contact is parsed into an email and a phone number, linked to a
res.partner (reused if one already has that email or number), and the lead is
marked with its source and catalog. Every step is best-effort: a failure
creating a helper record must never cost the customer their lead.

Everything here treats the customer's text as hostile. An email is matched
exactly (never with LIKE wildcards, which would let "%@gmail.com" grab a
stranger's record), only well-formed contacts become partners, names are capped,
and a partner reused under a different name is flagged for the human.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SOURCE_NAME = "Telegram"
CATALOG_TAG = {"cars": "Cars", "real_estate": "Real estate"}
NAME_MAX = 100
DUPLICATE_WINDOW_MINUTES = 10

_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_DATE_LIKE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")


def parse_contact(text: str | None) -> tuple[str | None, str | None]:
    """Pull an email and a phone number out of free text. Either may be None."""
    if not text:
        return None, None
    email_match = _EMAIL.search(text)
    email = email_match.group(0) if email_match else None
    remainder = _EMAIL.sub(" ", text)
    phone = None
    for candidate in _PHONE.findall(remainder):
        candidate = candidate.strip()
        if _DATE_LIKE.match(candidate):
            continue
        digits = re.sub(r"\D", "", candidate)
        if 7 <= len(digits) <= 15:
            phone = ("+" if candidate.startswith("+") else "") + digits
            break
    return email, phone


def merge_contact(model_contact: str | None, customer_text: str | None) -> str | None:
    """Add any email or phone the customer typed that the model left out.

    A model sometimes passes only the email to the lead tool although the customer
    also gave a number (or the reverse). Whatever the model provided is kept as is;
    only a *kind* of contact it is missing (email, phone) is filled in, and only
    from the customer's own words, never from anything the assistant said."""
    have_email, have_phone = parse_contact(model_contact)
    said_email, said_phone = parse_contact(customer_text)
    parts = [model_contact.strip()] if model_contact and model_contact.strip() else []
    if said_email and not have_email:
        parts.append(said_email)
    if said_phone and not have_phone:
        parts.append(said_phone)
    return " ".join(parts) or None


def _find_or_create(odoo, model: str, domain: list, values: dict):
    """Return the id of a matching record, creating it if there is none.
    Returns None instead of raising, so the caller can carry on without it."""
    try:
        found = odoo.search_read(model, domain, ["id"])
        if found:
            return found[0]["id"]
        return odoo.create(model, values)
    except Exception:
        logger.exception("could not find or create %s", model)
        return None


def _resolve_partner(odoo, name: str, email: str | None, phone: str | None):
    """Find the partner for this email or number, or create one.

    Returns (partner_id or None, a note for the human or None). If the partner
    on file has a different name, the lead is still linked to it but the
    difference is noted, since typing someone else's email is easy.
    """
    if email:
        domain = [("email_normalized", "=", email.lower())]
    else:
        domain = ["|", ("phone", "=", phone), ("phone_sanitized", "=", phone)]
    values = {"name": name}
    if email:
        values["email"] = email
    if phone:
        values["phone"] = phone
    try:
        found = odoo.search_read("res.partner", domain, ["id", "name"])
        if found:
            on_file = found[0].get("name") or ""
            note = None
            if on_file.strip().lower() != name.strip().lower():
                note = f"Name given ({name}) differs from the contact on file ({on_file})"
            return found[0]["id"], note
        return odoo.create("res.partner", values), None
    except Exception:
        logger.exception("could not find or create res.partner")
        return None, None


def find_recent_duplicate(odoo, args: dict) -> int | None:
    """The id of a lead this customer already opened for the same item in the last
    few minutes, if any. A customer who resends after an error, or a model that
    calls the tool twice, then gets the same lead instead of a second one. Only
    possible when there is a contact to compare, and never blocks a new lead."""
    email, phone = parse_contact(args.get("customer_contact"))
    contact_clauses = []
    if email:
        contact_clauses.append(("email_from", "=ilike", email.replace("_", r"\_").replace("%", r"\%")))
    if phone:
        contact_clauses.append(("phone", "=", phone))
    if not contact_clauses:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    domain = [("name", "=", args["name"]), ("create_date", ">=", cutoff)]
    if len(contact_clauses) == 2:
        domain += ["|", *contact_clauses]
    else:
        domain += contact_clauses
    try:
        found = odoo.search_read("crm.lead", domain, ["id"])
        return found[0]["id"] if found else None
    except Exception:
        logger.exception("duplicate check failed; creating the lead anyway")
        return None


def lead_contact_values(odoo, domain_type: str, args: dict) -> tuple[dict, list[str]]:
    """Return (crm.lead field values, extra description lines) for the contact,
    source and tag of a new lead."""
    values: dict = {}
    notes: list[str] = []

    name = (args.get("customer_name") or "").strip()[:NAME_MAX]
    if name:
        values["contact_name"] = name

    contact = args.get("customer_contact")
    email, phone = parse_contact(contact)
    if email:
        values["email_from"] = email
    if phone:
        values["phone"] = phone
    if contact and not email and not phone:
        notes.append(f"Contact as given: {contact}")

    if name and (email or phone):
        partner_id, note = _resolve_partner(odoo, name, email, phone)
        if partner_id:
            values["partner_id"] = partner_id
        if note:
            notes.append(note)

    source_id = _find_or_create(odoo, "utm.source", [("name", "=", SOURCE_NAME)], {"name": SOURCE_NAME})
    if source_id:
        values["source_id"] = source_id

    tag_name = CATALOG_TAG.get(domain_type)
    if tag_name:
        tag_id = _find_or_create(odoo, "crm.tag", [("name", "=", tag_name)], {"name": tag_name})
        if tag_id:
            values["tag_ids"] = [(6, 0, [tag_id])]

    return values, notes
