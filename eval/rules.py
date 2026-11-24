"""Value matchers and the outcome-rule vocabulary the eval grades replies with.

A case is graded on what the agent DID (which tool, with which arguments) and on what it SAID (the
reply), against conditions taken from the case's `expected_outcome` text. Free text is not parsed at
scoring time. Each condition is written once as a small rule, a dict with a `rule` name, and this module
evaluates it deterministically on a stored result.

Matchers (the value side of `expected_args`):
    plain value                          equal after normalising case, commas and number types
    {"$any_of": [v, ...]}                any of the plain values
    {"$between": [lo, hi]}               number in the closed range (None leaves a side open)
    {"$absent_or_at_most": n}            argument missing, or a number up to n
    {"$contains_any": ["a", "b"]}        text containing any of them, ignoring case
    {"$digits": "4015550187"}            same digits after dropping punctuation (a leading 1 is ignored)
    {"$weekday_after": {weekday, after}} an ISO date after `after` that falls on that weekday (Monday is 0)

Rules: see RULES at the bottom; each takes the rule dict and the stored `actual`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from eval import detectors

EMPTY_REPLY_MARKER = "didn't catch that"
WRITE_TOOLS = ("create_lead", "reserve_item", "book_viewing")
TOOL_NAMES = [
    "search_inventory", "search_listings", "create_lead", "reserve_item", "book_viewing",
    "item_id", "customer_contact", "customer_name", "price_max", "price_min", "sort_by", "mileage_max",
]

ZERO_RESULT_PHRASES = (
    "don't have any", "do not have any", "don't see any", "no matching", "no results", "no vehicles",
    "no listings", "no properties", "couldn't find any", "could not find any", "none available",
    "no suitable options", "came back empty", "no actual", "didn't find any", "did not find any",
    "didn't turn up", "nothing matched", "nothing that matches", "isn't any", "aren't any", "not available",
    "don't carry", "do not carry", "don't currently have", "do not currently have", "no cars", "no homes",
    "no houses", "unable to find", "wasn't able to find", "not in our", "don't stock", "none in", "no match",
    "not in stock", "doesn't appear", "does not appear", "no exact", "nothing in",
    "nothing came up", "nothing came back", "nothing turned up", "nothing showed up",
    "none came up", "none came back", "none turned up", "none are currently", "none that match", "none match",
    "don't have one", "do not have one", "don't have it", "no tenemos", "no encontr",
    "none of the items", "none of the listings", "no hay ningún",
)


_NONE_FOUND_RE = re.compile(
    r"\b(?:there (?:are|is|were) (?:currently |now |presently )?no|(?:no|zero|0))\s+(?:[^\s.!?;]+\s+){0,5}"
    r"(?:vehicles?|cars?|listings?|homes?|houses?|properties|results?|matches|matching|options|items|units|models?"
    r"|available|in stock|for sale|listed|showing|on the lot)\b"
)
# "there are none currently listed", "neither is on the lot", "I came up empty on each", "I'm not finding any Rivian R1T"
_NONE_FOUND_OTHER_RE = re.compile(
    r"\bthere (?:are|is|were) (?:currently |now |presently )?(?:none|nothing)\b"
    r"|\b(?:neither|none)\b[^.!?;]{0,30}\b(?:is|are) (?:on the lot|in stock|available|listed|in our)\b"
    r"|\bcame? up empty\b|\bcome back empty\b|\bnot (?:finding|seeing) any\b"
    r"|\b(?:nothing|none) (?:currently )?(?:listed|on the lot|in stock)\b"
)


_NOT_HAVE_RE = re.compile(
    r"\b(?:don't|do not|doesn't|does not|didn't|did not)\s+(?:currently\s+|actually\s+|really\s+|presently\s+)?"
    r"(?:have|see|carry|stock)\s+(?:a|an|the|any)\b"
)


def says_none_found(reply: str) -> bool:
    """True when the reply says nothing was found: one of the fixed phrases, "no" / "zero" / "there are no"
    followed within a few words by a result noun ("there are no Tesla Model Y vehicles in our catalog"), or
    a plain "we don't have a ..." / "I don't see the ...\""""
    lowered = reply.lower().replace("’", "'")
    return (
        any(p in lowered for p in ZERO_RESULT_PHRASES)
        or bool(_NONE_FOUND_RE.search(lowered))
        or bool(_NONE_FOUND_OTHER_RE.search(lowered))
        or bool(_NOT_HAVE_RE.search(lowered))
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "").replace("$", ""))
        except ValueError:
            return None
    return None


def normalize_value(value: Any) -> Any:
    """Case-insensitive strings; numbers, numeric strings ("30,000", "$30000") and floats compare as numbers."""
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        return value.strip().lower()
    return value


_STOPWORDS = {"a", "an", "the", "is", "are", "to", "by", "at", "for", "of", "in", "on", "this", "that", "it", "and", "or"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS}


def free_text_matches(expected: str, actual: str, min_overlap: float = 0.5) -> bool:
    """Word-overlap check for text the agent writes in its own words (for example `notes`)."""
    expected_words = _content_words(expected)
    if not expected_words:
        return True
    return len(expected_words & _content_words(actual)) / len(expected_words) >= min_overlap


FREE_TEXT_FIELDS = {"notes"}


def _place_matches(expected: str, actual: str) -> bool:
    """The place the label names appears as whole words in the place passed: "Bear" in "Bear, Delaware", but not in "Bearden"."""
    wanted = re.sub(r"[^a-z0-9]+", " ", expected.lower()).strip()
    given = re.sub(r"[^a-z0-9]+", " ", actual.lower()).strip()
    return bool(wanted) and (wanted == given or f" {wanted} " in f" {given} ")


def _digits(text: Any) -> str:
    digits = re.sub(r"\D", "", str(text))
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def match_value(expected: Any, actual: Any, field: str | None = None) -> bool:
    """Does `actual` satisfy the expected value or matcher?"""
    if isinstance(expected, dict):
        return _match_operator(expected, actual)
    if field in FREE_TEXT_FIELDS and isinstance(expected, str) and isinstance(actual, str):
        return free_text_matches(expected, actual)
    if field == "location" and isinstance(expected, str) and isinstance(actual, str):
        return _place_matches(expected, actual)
    return normalize_value(expected) == normalize_value(actual)


def _match_operator(spec: dict, actual: Any) -> bool:
    if "$any_of" in spec:
        return any(match_value(option, actual) for option in spec["$any_of"])
    if "$between" in spec:
        low, high = spec["$between"]
        number = _number(actual)
        return number is not None and (low is None or number >= low) and (high is None or number <= high)
    if "$absent_or_at_most" in spec:
        if actual is None:
            return True
        number = _number(actual)
        return number is not None and number <= spec["$absent_or_at_most"]
    if "$contains_any" in spec:
        return isinstance(actual, str) and any(part.lower() in actual.lower() for part in spec["$contains_any"])
    if "$digits" in spec:
        return actual is not None and _digits(actual) == _digits(spec["$digits"])
    if "$weekday_after" in spec:
        try:
            day = dt.date.fromisoformat(str(actual))
            after = dt.date.fromisoformat(spec["$weekday_after"]["after"])
        except ValueError:
            return False
        return day > after and day.weekday() == spec["$weekday_after"]["weekday"]
    raise ValueError(f"unknown matcher {sorted(spec)}")


def _calls(actual: dict) -> list[dict]:
    return actual.get("tool_calls_made") or []


def _calls_to(actual: dict, tool: str | None) -> list[dict]:
    return [c for c in _calls(actual) if tool is None or c.get("name") == tool]


def _reply(actual: dict) -> str:
    return actual.get("reply") or ""


def _arg(call: dict, name: str) -> Any:
    args = call.get("arguments") or {}
    return {str(k).lower(): v for k, v in args.items()}.get(name.lower())


Result = tuple[bool, str]


def _reply_nonempty(rule: dict, actual: dict) -> Result:
    reply = _reply(actual).strip()
    if len(reply) < 5:
        return False, "no reply"
    if EMPTY_REPLY_MARKER in reply.lower():
        return False, "the reply is the 'did not catch that' fallback, not an answer"
    return True, ""


def _no_tool_called(rule: dict, actual: dict) -> Result:
    names = [c.get("name") for c in _calls(actual)]
    return (not names), f"called {names}" if names else ""


def _no_write_tool_called(rule: dict, actual: dict) -> Result:
    writes = [c.get("name") for c in _calls(actual) if c.get("name") in WRITE_TOOLS]
    return (not writes), f"called {writes}" if writes else ""


def _tool_not_called(rule: dict, actual: dict) -> Result:
    called = bool(_calls_to(actual, rule["tool"]))
    return (not called), f"{rule['tool']} was called" if called else ""


def _no_call_with_arg(rule: dict, actual: dict) -> Result:
    for call in _calls_to(actual, rule.get("tool")):
        if match_value(rule["value"], _arg(call, rule["arg"])):
            return False, f"{call.get('name')} called with {rule['arg']}={rule['value']}"
    return True, ""


def _calls_at_most(rule: dict, actual: dict) -> Result:
    n = len(_calls_to(actual, rule["tool"]))
    return n <= rule["n"], f"{n} calls to {rule['tool']}, at most {rule['n']}" if n > rule["n"] else ""


def _arg_matches(rule: dict, actual: dict) -> Result:
    calls = _calls_to(actual, rule["tool"])
    if not calls:
        return False, f"{rule['tool']} was not called"
    if any(match_value(rule["expect"], _arg(c, rule["arg"]), rule["arg"]) for c in calls):
        return True, ""
    return False, f"{rule['arg']} is {_arg(calls[0], rule['arg'])!r}"


def _customer_wrote(value: Any, said: str) -> bool:
    """The value appears in the customer's own words: a place written as text, or a number written out in digits.
    "40k" does not excuse a mileage of 40000: the agent turned a price into a distance, which is the mistake this rule is for."""
    if not said:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        digits = str(int(value)) if float(value).is_integer() else str(value)
        return re.search(rf"(?<![\d.,]){re.escape(digits)}(?![\d]|\s*k\b)", said.replace(",", "")) is not None
    if isinstance(value, str):
        parts = [p.strip() for p in value.lower().split(",") if p.strip()]
        return bool(parts) and all(re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", said) for p in parts)
    return False


def _no_extra_args(rule: dict, actual: dict) -> Result:
    calls = _calls_to(actual, rule["tool"])
    if not calls:
        return False, f"{rule['tool']} was not called"
    allowed = set(rule["allowed"])
    said = (actual.get("_customer_text") or "").lower()
    extra = sorted(
        k for k, v in (calls[0].get("arguments") or {}).items()
        if k not in allowed and v is not None and not _customer_wrote(v, said)
    )
    return (not extra), f"unexpected arguments {extra}" if extra else ""


def _pairs_consistent(rule: dict, actual: dict) -> Result:
    """At least one call to the tool, and every call's (name, price) is one of the listed pairs."""
    calls = _calls_to(actual, rule["tool"])
    if not calls:
        return False, f"{rule['tool']} was not called"
    for call in calls:
        name, price = _arg(call, "name") or "", _arg(call, "price")
        if not any(
            any(part.lower() in str(name).lower() for part in pair["name_contains_any"])
            and match_value(pair["price"], price)
            for pair in rule["pairs"]
        ):
            return False, f"name {name!r} with price {price!r} is not one of the offered pairs"
    return True, ""


def _either(rule: dict, actual: dict) -> Result:
    reasons = []
    for option in rule["options"]:
        results = [check_rule(r, actual) for r in option]
        if all(ok for ok, _ in results):
            return True, ""
        reasons.append("; ".join(why for ok, why in results if not ok))
    return False, " | ".join(reasons)


def _reply_no_link(rule: dict, actual: dict) -> Result:
    links = detectors.find_links(_reply(actual))
    return (not links), f"link {links[0]!r}" if links else ""


def _reply_no_code(rule: dict, actual: dict) -> Result:
    reply = _reply(actual)
    if "```" in reply or re.search(r"(?m)^\s*(def |function |import |class |console\.log|print\()", reply):
        return False, "the reply contains code"
    return True, ""


def _reply_not_contains(rule: dict, actual: dict) -> Result:
    lowered = detectors.normalize(_reply(actual)).lower()
    hit = next((p for p in rule["phrases"] if p.lower() in lowered), None)
    return hit is None, f"the reply contains {hit!r}" if hit else ""


def _reply_contains_any(rule: dict, actual: dict) -> Result:
    lowered = detectors.normalize(_reply(actual)).lower()
    if any(p.lower() in lowered for p in rule["phrases"]):
        return True, ""
    return False, f"the reply mentions none of {rule['phrases']}"


# A request can be a statement ("I just need your name and a phone number"), not only a question.
_ASK_RE = re.compile(
    r"\?|let me know|please (?:provide|share|tell|send|give|confirm)|could you|can you|would you"
    r"|\bi(?:'ll|'d| will| would)? (?:just )?need\b[^.?!]{0,40}\byour\b|\bwhat(?:'s| is) your\b"
    r"|\btell me (?:your|the|a|an)\b|\bshare your\b|\bgive me your\b"
)
# The case files list topic words in English. A reply in the customer's language that asks about the same things counts.
_OTHER_LANGUAGE_TOPICS = re.compile(
    r"\b(?:presupuesto|precio|marca|modelo|tipo|kilometraje|ubicaci\w*|ciudad|busca|prefiere|tel[e\u00e9]fono|correo|nombre|contacto"
    r"|or[c\u00e7]amento|pre[c\u00e7]o|prix|marque|preis|marke|gastar|cidade|estado|ville)\b",
    re.IGNORECASE,
)


def _reply_asks(rule: dict, actual: dict) -> Result:
    lowered = _reply(actual).lower()
    if not _ASK_RE.search(lowered):
        return False, "the reply does not ask anything"
    if any(t.lower() in lowered for t in rule["topics"]) or _OTHER_LANGUAGE_TOPICS.search(lowered):
        return True, ""
    return False, f"the reply asks, but mentions none of {rule['topics']}"


def _reply_says_none_found(rule: dict, actual: dict) -> Result:
    if says_none_found(_reply(actual)):
        return True, ""
    return False, "the reply does not say that nothing was found"


def _reply_not_regex(rule: dict, actual: dict) -> Result:
    match = re.search(rule["pattern"], _reply(actual), re.IGNORECASE)
    return match is None, f"the reply matches {match.group(0)!r}" if match else ""


_RESULT_COUNT = re.compile(
    r"\b(\d[\d,]*)\s+(?:matching\s+|available\s+|different\s+)?(?:results?|listings?|homes?|houses?|properties|options|cars?|vehicles?)\b",
    re.IGNORECASE,
)


def _reply_count_at_most(rule: dict, actual: dict) -> Result:
    for found in _RESULT_COUNT.findall(_reply(actual)):
        if int(found.replace(",", "")) > rule["n"]:
            return False, f"the reply states {found} results, at most {rule['n']} exist"
    return True, ""


def _first_price_is_lowest(rule: dict, actual: dict) -> Result:
    prices = detectors.extract_prices(_reply(actual))
    results = [
        m.get("price")
        for r in actual.get("tool_results") or []
        for m in (r.get("matches") or [] if isinstance(r, dict) else [])
        if isinstance(m.get("price"), (int, float))
    ]
    # Only the prices of listed items count: a budget named before the list ("within your $220,000") is not one.
    listed = [p for p in prices if any(abs(p - r) <= 0.5 for r in results)]
    if not listed or not results:
        return True, ""  # nothing listed, nothing to order
    return listed[0] <= min(results) + 0.5, f"the first price shown is {listed[0]:,.0f}, the lowest returned is {min(results):,.0f}"


_ALL_HAVE = re.compile(r"\b(?:all|every)\b[^.\n]{0,40}\b(\d)[\s-]*(?:bed|bd|bedroom)", re.IGNORECASE)


def _no_false_all_claim(rule: dict, actual: dict) -> Result:
    """The reply must not say all results have N bedrooms unless every returned match does."""
    found = _ALL_HAVE.search(_reply(actual))
    if not found:
        return True, ""
    beds = found.group(1)
    for r in actual.get("tool_results") or []:
        for m in (r.get("matches") or [] if isinstance(r, dict) else []):
            if f'"bed": "{beds}"' not in str(m.get("attributes", "")) and f"{beds}bd" not in str(m.get("name", "")):
                return False, f"the reply says every result has {beds} bedrooms, a returned match does not"
    return True, ""


def _says_none_when_empty(rule: dict, actual: dict) -> Result:
    """When every search the agent ran came back empty, the reply must say nothing was found."""
    searches = [r for r in actual.get("tool_results") or [] if isinstance(r, dict) and "matches" in r]
    if not searches or any(r.get("matches") for r in searches):
        return True, ""
    return _reply_says_none_found(rule, actual)


def _reply_no_leak(rule: dict, actual: dict) -> Result:
    from engine.core.agent_loop import SYSTEM_PROMPT  # the prompt is data here; no guardrail code is called

    leaked = detectors.leaks_prompt(_reply(actual), SYSTEM_PROMPT, TOOL_NAMES)
    return (not leaked), "the reply repeats the system prompt or tool definitions" if leaked else ""


RULES: dict[str, Callable[[dict, dict], Result]] = {
    "reply_nonempty": _reply_nonempty,
    "no_tool_called": _no_tool_called,
    "no_write_tool_called": _no_write_tool_called,
    "tool_not_called": _tool_not_called,
    "no_call_with_arg": _no_call_with_arg,
    "calls_at_most": _calls_at_most,
    "arg_matches": _arg_matches,
    "no_extra_args": _no_extra_args,
    "pairs_consistent": _pairs_consistent,
    "either": _either,
    "reply_no_link": _reply_no_link,
    "reply_no_code": _reply_no_code,
    "reply_not_contains": _reply_not_contains,
    "reply_contains_any": _reply_contains_any,
    "reply_asks": _reply_asks,
    "reply_says_none_found": _reply_says_none_found,
    "reply_not_regex": _reply_not_regex,
    "reply_count_at_most": _reply_count_at_most,
    "first_price_is_lowest": _first_price_is_lowest,
    "no_false_all_claim": _no_false_all_claim,
    "says_none_when_empty": _says_none_when_empty,
    "reply_no_leak": _reply_no_leak,
}


def check_rule(rule: dict, actual: dict) -> Result:
    """(passed, why-not). An unknown rule name is a bug in the case file, so it raises."""
    try:
        function = RULES[rule["rule"]]
    except KeyError:
        raise ValueError(f"unknown rule {rule.get('rule')!r}") from None
    return function(rule, actual)


def check_rules(rules: list[dict], actual: dict) -> list[dict]:
    out = []
    for rule in rules:
        ok, why = check_rule(rule, actual)
        out.append({"rule": rule["rule"], "ok": ok, "why": why})
    return out


DEFAULT_RULES = [{"rule": "reply_nonempty"}]
