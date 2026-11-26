"""Telegram inline buttons under a list of results: pick an item, then an action, then (for a viewing) a day and a time.

A tap carries only an action and an item id (Telegram allows 64 bytes of callback data) and is checked against the catalog
when it arrives. A tap that asks for a hold, viewing or callback does not act by itself: it becomes the sentence the customer
would have typed ("I'd like to see the 2024 Cadillac XT5 on Saturday 2026-11-28 in the morning.") and runs as a normal turn,
so the name and contact are still asked for and every guard still runs.
"""

import datetime as dt
import json
import re

from engine.core.paging import PAGE_SIZE
from engine.core.tools import VIEWING_SLOTS

PICK, ACT, DAY, SLOT, MORE = "pick", "act", "day", "slot", "more"
ACTIONS = ("hold", "view", "call")
SLOT_LABELS = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}
DAYS_OFFERED = 6

LABEL_MAX = 56
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OFFSET = re.compile(r"[1-9][0-9]{0,3}")  # ASCII digits only
_KEY = re.compile(r"[0-9a-f]{8}")


def _button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _money(value) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return ""


def item_label(row: dict) -> str:
    """The text on an item's button: its name and price, cut to fit."""
    label = f"{row.get('name', 'Item')} · {_money(row.get('price'))}".rstrip(" ·")
    return label if len(label) <= LABEL_MAX else label[: LABEL_MAX - 1].rstrip() + "…"


def actions_keyboard(item_id: int) -> dict:
    return {
        "inline_keyboard": [
            [_button("Hold it", f"{ACT}:hold:{item_id}"), _button("Book a viewing", f"{ACT}:view:{item_id}")],
            [_button("Have someone call me", f"{ACT}:call:{item_id}")],
        ]
    }


def results_keyboard(shown: list[dict], more: dict | None = None) -> dict | None:
    """One button per item the reply described, in that order. A single item goes straight to its actions. `more`
    ({"offset", "key"}) adds a last row for the next page of the same search."""
    items = [row for row in shown if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)][:PAGE_SIZE]
    if not items:
        return None
    if len(items) == 1:
        keyboard = actions_keyboard(items[0]["id"])["inline_keyboard"]
    else:
        keyboard = [[_button(item_label(row), f"{PICK}:{row['id']}")] for row in items]
    if more:
        keyboard = [*keyboard, [_button(f"Next {PAGE_SIZE}", f"{MORE}:{more['offset']}:{more['key']}")]]
    return {"inline_keyboard": keyboard}


def page_text(offset: int, count: int, rows: list[dict]) -> str:
    """A further page of a search, written from the catalog rows. Items are numbered from 1 on every page, so "the
    second one" means the second one on screen."""
    lines = [f"Matches {offset + 1} to {offset + len(rows)} of {count}:"]
    for number, row in enumerate(rows, 1):
        lines.append(f"{number}. {item_card(row).replace(chr(10), ' - ', 1)}")
    return "\n".join(lines)


def day_keyboard(item_id: int, today: dt.date) -> dict:
    """The next few days, two to a row: Today, Tomorrow, then the weekday and date."""
    buttons = []
    for offset in range(DAYS_OFFERED):
        day = today + dt.timedelta(days=offset)
        label = "Today" if offset == 0 else "Tomorrow" if offset == 1 else f"{day:%a} {day.day} {day:%b}"
        buttons.append(_button(label, f"{DAY}:{item_id}:{day.isoformat()}"))
    return {"inline_keyboard": [buttons[i : i + 2] for i in range(0, len(buttons), 2)]}


def slot_keyboard(item_id: int, day: str) -> dict:
    return {"inline_keyboard": [[_button(SLOT_LABELS[slot], f"{SLOT}:{item_id}:{day}:{slot}") for slot in VIEWING_SLOTS]]}


def parse_callback(data) -> tuple[str, int, list[str]] | None:
    """(kind, item id, the rest) for a tap this module made, else None. Strict about every part, so nothing else is
    accepted. For a next-page tap the number is the offset and the rest is the search's key."""
    if not isinstance(data, str) or len(data.encode()) > 64:
        return None
    parts = data.split(":")
    kind = parts[0]
    try:
        if kind == MORE and len(parts) == 3 and _OFFSET.fullmatch(parts[1]) and _KEY.fullmatch(parts[2]):
            return MORE, int(parts[1]), [parts[2]]
        if kind == PICK and len(parts) == 2:
            return PICK, _id(parts[1]), []
        if kind == ACT and len(parts) == 3 and parts[1] in ACTIONS:
            return ACT, _id(parts[2]), [parts[1]]
        if kind == DAY and len(parts) == 3 and _ISO.fullmatch(parts[2]):
            dt.date.fromisoformat(parts[2])
            return DAY, _id(parts[1]), [parts[2]]
        if kind == SLOT and len(parts) == 4 and _ISO.fullmatch(parts[2]) and parts[3] in VIEWING_SLOTS:
            dt.date.fromisoformat(parts[2])
            return SLOT, _id(parts[1]), [parts[2], parts[3]]
    except ValueError:
        return None
    return None


def _id(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,12}", text):  # ASCII digits only: str.isdigit() also accepts other scripts
        raise ValueError("not an item id")
    return int(text)


def _attributes(row: dict) -> dict:
    raw = row.get("attributes")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value) -> str | None:
    try:
        return f"{float(str(value).replace(',', '')):,.0f}"
    except ValueError:
        return None


def item_card(row: dict) -> str:
    """A short description built from the catalog row alone: name, price and the facts the catalog has for it."""
    attrs = _attributes(row)
    facts = [_money(row.get("price"))]
    if attrs.get("mileage") and _number(attrs["mileage"]):
        facts.append(f"{_number(attrs['mileage'])} miles")
    if attrs.get("condition"):
        facts.append(str(attrs["condition"]))
    if attrs.get("bed"):
        facts.append(f"{attrs['bed']} bed")
    if attrs.get("bath"):
        facts.append(f"{attrs['bath']} bath")
    if attrs.get("house_size") and _number(attrs["house_size"]):
        facts.append(f"{_number(attrs['house_size'])} sq ft")
    place = attrs.get("location") or ", ".join(str(attrs[k]) for k in ("city", "state") if attrs.get(k))
    if place:
        facts.append(str(place))
    return f"{row.get('name', 'This item')}\n{' · '.join(f for f in facts if f)}"


def pick_text(row: dict) -> str:
    return f"{item_card(row)}\n\nWhat would you like to do?"


def day_text(row: dict) -> str:
    return f"Which day would you like to see the {row.get('name', 'item')}?"


def slot_text(day: str) -> str:
    return f"And what part of the day on {_pretty_day(day)}?"


def _pretty_day(day: str) -> str:
    date = dt.date.fromisoformat(day)
    return f"{date:%A} {date.day} {date:%B}"


def customer_words(kind: str, row: dict, parts: list[str]) -> str | None:
    """What a tap that acts says on the customer's behalf, in the words they would have typed; None for taps that only
    navigate. The date is written as a weekday and an ISO date, the time as a word, so the viewing guard sees both."""
    name = row.get("name", "this item")
    if kind == ACT and parts == ["hold"]:
        return f"Please hold the {name} for me."
    if kind == ACT and parts == ["call"]:
        return f"Please have someone call me about the {name}."
    if kind == SLOT:
        day, slot = parts
        date = dt.date.fromisoformat(day)
        return f"I'd like to see the {name} on {date:%A} {day} in the {slot}."
    return None
