"""Turn the placeholders in the frozen held-out labels into concrete expectations.

The held-out cases name catalog items by description ("<catalog id of the 2021 Land Rover Range Rover
priced 40470 in Cincinnati, OH>") and days relative to the run ("<the first Saturday after the harness
reference date>"). A run resolves them against the live catalog and a reference date, and stores the
result in each record, so scoring later needs neither the catalog nor the date.

A description that matches no item, or more than one, is a `ResolutionError`: the case is reported as
unresolvable, never guessed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}

_ITEM_RE = re.compile(r"^<catalog id of (?:the )?(?P<desc>.+)>$", re.IGNORECASE | re.DOTALL)
_DAY_RE = re.compile(r"^<the first (?P<day>[A-Za-z]+) after the harness reference date(?:,[^>]*)?>$", re.IGNORECASE)
_FILLER = {"the", "a", "an", "listing", "house", "home", "priced", "price", "at", "for", "in", "of", "residential"}


class ResolutionError(Exception):
    """A placeholder that cannot be turned into exactly one concrete value."""


def _attributes(row: dict) -> dict:
    raw = row.get("attributes")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _words(row: dict) -> set[str]:
    text = " ".join([str(row.get("name", ""))] + [str(v) for v in _attributes(row).values()])
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _beds_baths(row: dict) -> tuple[str | None, str | None]:
    attrs = _attributes(row)
    return (str(attrs["bed"]) if "bed" in attrs else None, str(attrs["bath"]) if "bath" in attrs else None)


def find_catalog_item(description: str, catalog: list[dict]) -> int:
    """The id of the one catalog row that fits `description`; ResolutionError for none or several.

    The price (a number of at least four digits, with or without commas) must equal the row's price.
    "N bed" and "N bath" must equal the row's bed and bath counts. Every other word must appear in the
    row's name or attributes.
    """
    text = description.strip()
    # The number after "priced" is the price; without it, the last number of four or more digits (a year comes first).
    price_match = re.search(r"\bpriced\s+\$?(\d[\d,]*)", text, re.IGNORECASE)
    if price_match is None:
        price_match = next(iter(reversed(list(re.finditer(r"\b(\d[\d,]{3,})\b", text)))), None)
    price =float(price_match.group(1).replace(",", "")) if price_match else None
    if price_match:
        text = text[: price_match.start()] + " " + text[price_match.end():]
    beds = re.search(r"\b(\d+)\s*bed(?:room)?s?\b", text, re.IGNORECASE)
    baths = re.search(r"\b(\d+)\s*bath(?:room)?s?\b", text, re.IGNORECASE)
    for found in (beds, baths):
        if found:
            text = text.replace(found.group(0), " ")
    tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _FILLER]

    matches = []
    for row in catalog:
        if price is not None and abs(float(row.get("price", 0)) - price) > 0.5:
            continue
        row_beds, row_baths = _beds_baths(row)
        if beds and row_beds != beds.group(1):
            continue
        if baths and row_baths != baths.group(1):
            continue
        words = _words(row)
        if all(t in words for t in tokens):
            matches.append(row)
    if not matches:
        raise ResolutionError(f"no catalog item fits {description!r}")
    if len(matches) > 1:
        ids = ", ".join(str(m["id"]) for m in matches)
        raise ResolutionError(f"{len(matches)} items fit {description!r} (ids {ids})")
    return int(matches[0]["id"])


def next_weekday(reference: dt.date, weekday: int) -> dt.date:
    """The first date strictly after `reference` that falls on `weekday` (Monday is 0)."""
    days = (weekday - reference.weekday()) % 7
    return reference + dt.timedelta(days=days or 7)


def weekday_matcher(day_name: str, reference: dt.date) -> dict:
    """A matcher accepting any date after `reference` that falls on the named weekday."""
    return {"$weekday_after": {"weekday": WEEKDAYS[day_name.lower()], "after": reference.isoformat()}}


def resolve_value(value: Any, catalog: list[dict], reference: dt.date) -> Any:
    """A placeholder string becomes its concrete value; anything else is returned unchanged."""
    if not isinstance(value, str):
        return value
    item = _ITEM_RE.match(value.strip())
    if item:
        return find_catalog_item(item.group("desc"), catalog)
    if value.startswith("$weekday:") and value[9:].lower() in WEEKDAYS:
        return weekday_matcher(value[9:], reference)
    day = _DAY_RE.match(value.strip())
    if day and day.group("day").lower() in WEEKDAYS:
        return next_weekday(reference, WEEKDAYS[day.group("day").lower()]).isoformat()
    return value


def resolve_args(args: dict | None, catalog: list[dict], reference: dt.date) -> tuple[dict, list[str]]:
    """(resolved args, errors). An argument that cannot be resolved is left out and named in `errors`."""
    resolved: dict = {}
    errors: list[str] = []
    for key, value in (args or {}).items():
        try:
            resolved[key] = resolve_value(value, catalog, reference)
        except ResolutionError as exc:
            errors.append(f"{key}: {exc}")
    return resolved, errors
