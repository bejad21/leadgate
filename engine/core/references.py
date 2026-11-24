"""Which item the customer means by "the third one", "the last one" or "that one".

The model sometimes counts wrong, so the position is worked out in code from what the customer saw: the most recent
assistant message that named items, with the items in the order their prices appear in it. That holds whether the reply
numbered the items, used bullets or ran them together in one sentence.

No stored state is needed. Rows come from the search results in the history and from the current turn's searches, so a chat
restored after a restart (text only) resolves once the model has searched again.
"""

import json
import re
from collections.abc import Iterable

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
    "last": -1,
}
_ITEM_WORDS = r"(?:one|car|vehicle|home|house|listing|property|option|result|item|unit)"
# "the third one", "3rd car", "the last listing"; and "number 3", "#3", "option 2" (a bare "3 bedrooms" is not a position)
_WORD_POSITION = re.compile(
    rf"(?<![\w-])(?:the\s+)?(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)(?:\s+{_ITEM_WORDS})?(?![\w-])", re.IGNORECASE
)
_NUMBER_POSITION = re.compile(
    r"(?<![\w-])(?:(?:number|no\.?|option|item|result|choice)\s*#?\s*|#\s*)([1-9])(?![\d\w])(?!\s*(?:bed|bath|ba\b|bd\b|k\b|miles|mi\b))",
    re.IGNORECASE,
)
# "that one", "this one", "it": only meaningful when exactly one item was in view
_THAT_ONE = re.compile(r"(?<![\w-])(?:that one|this one|the one|that car|that home|that house|that listing|that property)(?![\w-])", re.IGNORECASE)


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def position_in(text: str) -> int | None:
    """The position a message names: 1 to 5, or -1 for "the last one". None when it names none."""
    text = text or ""
    word = _WORD_POSITION.search(text)
    if word:
        return _ORDINALS[word.group(1).lower()]
    number = _NUMBER_POSITION.search(text)
    if number:
        return int(number.group(1))
    return None


def refers_to_the_one_in_view(text: str) -> bool:
    return bool(_THAT_ONE.search(text or ""))


def _price_forms(price) -> list[str]:
    try:
        whole = int(round(float(price)))
    except (TypeError, ValueError):
        return []
    return [f"{whole:,}", str(whole)]


def _price_positions(text: str, price) -> list[int]:
    """Where `price` is written in `text`, in either 40,850 or 40850 form, not inside a longer number."""
    found: list[int] = []
    for form in _price_forms(price):
        for match in re.finditer(rf"(?<![\d,.]){re.escape(form)}(?![\d]|,\d|\.\d)", text):
            if match.start() not in found:
                found.append(match.start())
    return sorted(found)


def rows_in_history(history: list[dict]) -> list[dict]:
    """Every catalog row that a search result in the history carried, oldest first, one per id (the latest copy wins)."""
    by_id: dict[int, dict] = {}
    for message in history:
        if message.get("role") != "tool":
            continue
        try:
            data = json.loads(message.get("content") or "")
        except ValueError:
            continue
        for row in (data.get("matches") if isinstance(data, dict) else None) or []:
            if isinstance(row, dict) and _int_or_none(row.get("id")) is not None:
                by_id[row["id"]] = row
    return list(by_id.values())


def rows_in_results(results: Iterable) -> list[dict]:
    rows: list[dict] = []
    for result in results:
        for row in (result.get("matches") if isinstance(result, dict) else None) or []:
            if isinstance(row, dict) and _int_or_none(row.get("id")) is not None:
                rows.append(row)
    return rows


def order_by_appearance(text: str, rows: list[dict]) -> list[dict]:
    """The rows whose price is written in `text`, in the order they are written. Rows with the same price take the
    successive places where that price appears, in the order the rows were returned."""
    placed: list[tuple[int, int, dict]] = []
    used: dict[str, int] = {}
    for order, row in enumerate(rows):
        positions = _price_positions(text, row.get("price"))
        if not positions:
            continue
        key = f"{round(float(row['price']))}"
        index = used.get(key, 0)
        used[key] = index + 1
        placed.append((positions[min(index, len(positions) - 1)], order, row))
    placed.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in placed]


def last_listed(history: list[dict], extra_rows: Iterable[dict] = ()) -> list[dict]:
    """The items of the most recent assistant message that named any known item, in the order that message names them.
    Empty when no assistant message did."""
    pool: dict[int, dict] = {row["id"]: row for row in rows_in_history(history)}
    for row in extra_rows:
        if isinstance(row, dict) and _int_or_none(row.get("id")) is not None:
            pool[row["id"]] = row
    rows = list(pool.values())
    if not rows:
        return []
    for message in reversed(history):
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, str) or message.get("tool_calls"):
            continue
        ordered = order_by_appearance(content, rows)
        if ordered:
            return ordered
    return []


def resolve_reference(text: str, history: list[dict], extra_rows: Iterable[dict] = ()) -> dict | None:
    """The catalog row the customer's `text` points at by position ("the third one", "the last one") or, when exactly one
    item was in view, by "that one". None when the text points at nothing, or at a place the list does not have."""
    position = position_in(text)
    listed = last_listed(history, extra_rows)
    if position is not None:
        if len(listed) < 2:
            return None  # a position needs a list of at least two to mean anything
        if position == -1:
            return listed[-1]
        return listed[position - 1] if position <= len(listed) else None
    if refers_to_the_one_in_view(text) and len(listed) == 1:
        return listed[0]
    return None
