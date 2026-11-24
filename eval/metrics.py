"""Offline metrics for the LeadGate eval: per-case grading and summaries with confidence intervals.

Reads stored per-case result records (one per case and repeat; see `eval.results_io`) and never calls a
model, the engine or Odoo. Each record is graded on separate checks:

- tool_ok       the tool that carried out the request (the first non-search tool, else the first search) is the expected one, one of the accepted alternatives, or
                (when none is expected) no tool at all
- slot_ok       the arguments of the expected tool call match the labelled arguments (None when the case
                has nothing to grade)
- outcome_ok    every outcome rule written for the case passes (refusals, clarifying questions, "no match"
                replies, the "Fail if ..." conditions); the reply must also be a real answer
- grounding_ok  every checkable claim in the reply (prices, counts, mileage, bedrooms, years, places,
                "nothing found") is supported by the tool results (see `eval.claims`)
- case_pass     all four hold. It is reported next to the separate numbers, never instead of them.

A run that errored (provider failure) fails every check and is counted as an error. A case whose labels
could not be resolved against the catalog is excluded and counted separately.

`price_only_flag` keeps the earlier, narrower check (dollar amounts only) as its own number.
"""

from __future__ import annotations

import re

from eval import claims, rules, stats
from eval.stats import wilson

SEARCH_TOOLS = ("search_inventory", "search_listings")


def _first_tool_name(actual: dict) -> str | None:
    """The tool that carried out the request: the first tool that is not a search, else the first
    search, else None. Finding an item and then booking it is one correct move, so the search before
    a booking is not what is judged."""
    calls = actual.get("tool_calls_made") or []
    for c in calls:
        if c["name"] not in SEARCH_TOOLS:
            return c["name"]
    return calls[0]["name"] if calls else None


def _slot_fields(expected: dict, actual: dict) -> list[bool] | None:
    """Per-field results for the expected tool's labelled arguments, or None when there is nothing to grade.

    Nothing to grade: no tool expected, no arguments labelled, or the expected tool was never called
    (tool selection already counts that). The first call to the expected tool is the one graded. An
    expected value of None matches a missing argument. For create_lead the `name` is a title the agent
    composes and is not graded; customer name, contact and price are.
    """
    tool = expected.get("tool")
    labelled = expected.get("args") or {}
    if tool is None or not labelled:
        return None
    call = next((c for c in actual.get("tool_calls_made") or [] if c.get("name") == tool), None)
    if call is None:
        return None
    if tool == "create_lead":
        labelled = {k: v for k, v in labelled.items() if k != "name"}
        if not labelled:
            return None
    given = {str(k).lower(): v for k, v in (call.get("arguments") or {}).items()}
    return [rules.match_value(v, given.get(str(k).lower()), str(k).lower()) for k, v in labelled.items()]


# The earlier price-only check, kept as a separate number

_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s?([kKmM])?")
_SUGGESTION_KEYWORDS = ("refine", "narrow", "would you like", "expand your search", "widen", "ballpark", "in mind")


def _parse_money(text: str, skip_suggestions: bool = True) -> set[float]:
    amounts = set()
    sentences = re.split(r"(?<=[.!?])\s+", text) if skip_suggestions else [text]
    for sentence in sentences:
        if skip_suggestions and any(kw in sentence.lower() for kw in _SUGGESTION_KEYWORDS):
            continue
        for match in _MONEY_RE.finditer(sentence):
            try:
                value = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if match.group(2) and match.group(2).lower() == "k":
                value *= 1_000
            elif match.group(2) and match.group(2).lower() == "m":
                value *= 1_000_000
            amounts.add(value)
    return amounts


def price_only_flag(record: dict) -> bool | None:
    """True when a reply to a search states a dollar amount that no result, label ceiling or customer figure
    supports (6 % rounding allowed), None when the agent ran no search."""
    actual = record["actual"]
    searches = [
        tr
        for call, tr in zip(actual.get("tool_calls_made") or [], actual.get("tool_results") or [], strict=False)
        if call.get("name") in SEARCH_TOOLS and isinstance(tr, dict)
    ]
    if not searches:
        return None
    grounded: set[float] = set()
    for tr in searches:
        grounded |= {round(float(m["price"]), 2) for m in tr.get("matches", []) if isinstance(m.get("price"), (int, float))}
    ceiling = (record["expected"].get("args") or {}).get("price_max")
    if isinstance(ceiling, (int, float)):
        grounded.add(float(ceiling))
    grounded |= _parse_money(record.get("message", ""))
    reply = actual.get("reply") or ""
    for amount in _parse_money(reply):
        if not any(g and abs(amount - g) / g <= 0.06 or amount == g for g in grounded):
            return True
    return False


# Grading


def _context_text(record: dict) -> str:
    parts = [record.get("message", "")]
    parts += [m.get("content") or "" for m in record.get("history") or [] if isinstance(m.get("content"), str)]
    return " ".join(parts)


def _resolved_rules(rule_list: list[dict], args: dict) -> list[dict]:
    """A rule written with a day name ("$weekday:tuesday") gets the same dated matcher the label's own
    expected arguments carry for that argument. A label author could write the day in either place."""
    out = []
    for rule in rule_list:
        expect = rule.get("expect")
        if rule.get("rule") == "arg_matches" and isinstance(expect, str) and expect.startswith("$weekday:"):
            matcher = args.get(rule.get("arg"))
            if isinstance(matcher, dict) and "$weekday_after" in matcher:
                rule = {**rule, "expect": matcher}
        out.append(rule)
    return out


def grade_record(record: dict) -> dict:
    """Grade one stored result. See the module docstring for the meaning of each key."""
    expected, actual = record["expected"], record["actual"]
    base = {"case_id": record.get("case_id"), "repeat": record.get("repeat", 0), "category": record.get("category"),
            "latency_ms": record.get("latency_ms")}
    if expected.get("resolution_errors"):
        return {**base, "excluded": True, "error": False, "tool_ok": None, "slot_ok": None, "slot_fields": None,
                "outcome_ok": None, "rule_results": [], "grounding_ok": None, "claims": [], "price_only_flag": None,
                "case_pass": None}
    if actual.get("error"):
        return {**base, "excluded": False, "error": True, "tool_ok": False, "slot_ok": None, "slot_fields": None,
                "outcome_ok": False, "rule_results": [], "grounding_ok": None, "claims": [], "price_only_flag": None,
                "case_pass": False}

    first = _first_tool_name(actual)
    allowed = {expected.get("tool"), *expected.get("accept_tools", [])}
    tool_ok = first in allowed
    # A hold, lead or viewing the case asks for must have been made, not only attempted: a refused call is no hold.
    wanted_writes = {name for name in allowed if name in rules.WRITE_TOOLS}
    write_ok = True
    if tool_ok and first in wanted_writes:
        results = actual.get("tool_results") or []
        write_ok = any(
            call.get("name") in wanted_writes and not (isinstance(result, dict) and "error" in result)
            for call, result in zip(actual.get("tool_calls_made") or [], results, strict=False)
        )
    fields = _slot_fields(expected, actual)
    slot_ok = None if fields is None else all(fields)
    rule_results = rules.check_rules(
        _resolved_rules(expected.get("rules") or [], expected.get("args") or {}),
        {**actual, "_customer_text": _context_text(record)},
    )
    outcome_ok = all(r["ok"] for r in rule_results)
    found = claims.check_reply(
        actual.get("reply") or "",
        tool_results=actual.get("tool_results") or [],
        tool_calls=actual.get("tool_calls_made") or [],
        context_text=_context_text(record),
        evidence_rows=actual.get("evidence_rows"),
    )
    grounding_ok = all(c["grounded"] for c in found)
    return {
        **base, "excluded": False, "error": False, "tool_ok": tool_ok, "slot_ok": slot_ok, "slot_fields": fields,
        "outcome_ok": outcome_ok, "rule_results": rule_results, "grounding_ok": grounding_ok, "claims": found,
        "price_only_flag": price_only_flag(record),
        "write_ok": write_ok,
        "case_pass": bool(tool_ok and write_ok and slot_ok is not False and outcome_ok and grounding_ok),
    }


def grade_all(records: list[dict]) -> list[dict]:
    return [grade_record(r) for r in records]


# Summaries


def _prop(successes: int, n: int) -> dict:
    p, low, high = wilson(successes, n)
    return {"successes": successes, "n": n, "p": p, "low": low, "high": high}


def _share(graded: list[dict], key: str, *, skip_errors: bool = False) -> dict:
    pool = [g for g in graded if g[key] is not None and not (skip_errors and g["error"])]
    return _prop(sum(1 for g in pool if g[key]), len(pool))


def summarize_records(graded: list[dict], k: int) -> dict:
    """Every metric with its Wilson interval, plus pass@1 and pass^k over `k` repeats, errors and by category."""
    scored = [g for g in graded if not g["excluded"]]
    fields = [f for g in scored if g["slot_fields"] for f in g["slot_fields"]]
    latencies = [g["latency_ms"] for g in scored if not g["error"] and g["latency_ms"] is not None]
    mean, low, high = stats.mean_ci(latencies)
    flagged = [g for g in scored if g["price_only_flag"] is not None]
    return {
        "n_records": len(graded),
        "n_scored": len(scored),
        "n_errors": sum(1 for g in scored if g["error"]),
        "n_unresolvable": sum(1 for g in graded if g["excluded"]),
        "tool_selection": _share(scored, "tool_ok"),
        "slot_extraction": _share(scored, "slot_ok"),
        "slot_fields": _prop(sum(fields), len(fields)),
        "outcome": _share(scored, "outcome_ok"),
        "grounding": _share(scored, "grounding_ok", skip_errors=True),
        "n_with_claims": sum(1 for g in scored if g["claims"]),
        "case_pass": _share(scored, "case_pass"),
        "price_only_ungrounded": _prop(sum(1 for g in flagged if g["price_only_flag"]), len(flagged)),
        "pass_k": stats.pass_at_1_and_pass_k([(g["case_id"], bool(g["case_pass"])) for g in scored], k),
        "latency_ms": {"mean": mean, "low": low, "high": high, "n": len(latencies)},
        "by_category": {
            category: {
                "tool_selection": _share(group, "tool_ok"),
                "outcome": _share(group, "outcome_ok"),
                "case_pass": _share(group, "case_pass"),
                "n_cases": len({g["case_id"] for g in group}),
            }
            for category in sorted({g["category"] for g in scored if g["category"]})
            for group in [[g for g in scored if g["category"] == category]]
        },
    }

