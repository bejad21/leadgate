"""Offline metrics for the LeadGate evaluation harness.

Reads the per-case result records in `eval/results/*.json` and computes five metrics:

- tool_selection_accuracy: did the agent call the right tool (or correctly call none for
  out-of-scope requests)?
- slot_extraction_accuracy: when a tool call was expected, were the right argument values extracted?
- hallucination_rate: when a search tool was called, does the reply state only facts (prices,
  "no results") that its results support?
- task_completion_rate: a strict composite of the above plus a non-trivial reply; the most
  subjective metric.
- mean_latency_ms: mean latency recorded per case.

No API or database calls are made; this is pure computation over saved JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any



def load_results(path: str | Path) -> list[dict]:
    """Load a results JSON file (a list of per-case result dicts)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)



def _first_tool_name(actual: dict) -> str | None:
    """The name of the first tool call made, or None if none was made."""
    calls = actual.get("tool_calls_made") or []
    return calls[0]["name"] if calls else None


def tool_selection_accuracy(results: list[dict]) -> float:
    """Fraction of cases where the first tool called matches the expected tool.

    "No tool expected, none called" counts as a match. Only the first call is compared, since fixtures
    encode one expected tool per case, so a case that makes an extra follow-up call still scores
    correct if its first call matches.
    """
    if not results:
        return 0.0
    correct = 0
    for r in results:
        expected_tool = r["expected"].get("tool")
        actual_tool = _first_tool_name(r["actual"])
        if expected_tool == actual_tool:
            correct += 1
    return correct / len(results)



def _normalize_value(value: Any) -> Any:
    """Normalize a slot value for comparison: case-insensitive strings, numerically tolerant.

    Forgiving about representation only: "30000", 30000 and "30,000" match, "Toyota" and "toyota"
    match, "Toyota" and "Honda" do not.
    """
    if isinstance(value, str):
        stripped = value.strip().lower()
        numeric_stripped = stripped.replace(",", "").replace("$", "")
        try:
            return float(numeric_stripped)
        except ValueError:
            return stripped
    if isinstance(value, (int, float)):
        return float(value)
    return value


_STOPWORDS = {
    "a", "an", "the", "is", "are", "to", "by", "at", "for", "of", "in", "on",
    "this", "that", "it", "and", "or",
}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS}


def _free_text_matches(expected_value: str, actual_value: str, min_overlap: float = 0.5) -> bool:
    """Word-overlap check for free-text fields the agent writes in its own words (for example `notes`).

    Exact equality would fail "Needs the car by next month" against "Customer needs the vehicle by next
    month". Most expected content words must still appear, so a note citing a wrong fact fails.
    """
    expected_words = _content_words(expected_value)
    if not expected_words:
        return True
    actual_words = _content_words(actual_value)
    overlap = len(expected_words & actual_words) / len(expected_words)
    return overlap >= min_overlap


# Agent-composed free text, graded by word overlap instead of exact equality. Only `create_lead` has such arguments.
_FREE_TEXT_FIELDS = {"notes"}


def _values_match(expected_value: Any, actual_value: Any, field_name: str | None = None) -> bool:
    if (
        field_name in _FREE_TEXT_FIELDS
        and isinstance(expected_value, str)
        and isinstance(actual_value, str)
    ):
        return _free_text_matches(expected_value, actual_value)
    return _normalize_value(expected_value) == _normalize_value(actual_value)


def _find_matching_call(calls: list[dict], tool_name: str) -> dict | None:
    for call in calls:
        if call.get("name") == tool_name:
            return call
    return None


def _case_slot_fields(r: dict) -> list[bool] | None:
    """Per-field match results (True/False) for one case's expected tool arguments, or None if the case
    is not gradable here.

    None means no tool or no args were expected, or the expected tool was never called. The last is a
    tool-selection failure that `tool_selection_accuracy` already counts, so it is not penalized twice.

    Shared by `slot_extraction_accuracy` and `task_completion_rate` so they cannot drift apart on what
    counts as a match. If a turn has several calls, the first one matching the expected tool is used.
    An expected value of `None` (for example `price_max: null`) matches when the key is absent.

    For `create_lead`, the `name` argument is not graded: it is a title the agent composes, and the
    eval dataset says to grade customer_name, customer_contact and price instead.
    """
    expected = r["expected"]
    expected_tool = expected.get("tool")
    expected_args = expected.get("args") or {}
    if expected_tool is None or not expected_args:
        return None

    calls = r["actual"].get("tool_calls_made") or []
    matching_call = _find_matching_call(calls, expected_tool)
    if matching_call is None:
        return None

    actual_args = matching_call.get("arguments") or {}
    actual_lookup = {str(k).lower(): v for k, v in actual_args.items()}

    graded_args = expected_args
    if expected_tool == "create_lead":
        graded_args = {k: v for k, v in expected_args.items() if k != "name"}
        if not graded_args:
            return None

    return [
        _values_match(expected_value, actual_lookup.get(str(key).lower()), field_name=str(key).lower())
        for key, expected_value in graded_args.items()
    ]


def slot_extraction_stats(results: list[dict]) -> dict:
    """Breakdown behind `slot_extraction_accuracy`: field counts, applicable cases, and why others were
    excluded.

    Reported so the accuracy is never read without its denominator.
    """
    total_fields = 0
    correct_fields = 0
    n_applicable_cases = 0
    n_excluded_not_applicable = 0  # no tool expected, or no args to check
    n_excluded_tool_mismatch = 0  # tool call expected but never made

    for r in results:
        expected = r["expected"]
        expected_tool = expected.get("tool")
        expected_args = expected.get("args") or {}

        fields = _case_slot_fields(r)
        if fields is None:
            if expected_tool is None or not expected_args:
                n_excluded_not_applicable += 1
            else:
                n_excluded_tool_mismatch += 1
            continue

        n_applicable_cases += 1
        total_fields += len(fields)
        correct_fields += sum(fields)

    return {
        "accuracy": correct_fields / total_fields if total_fields else 0.0,
        "total_fields": total_fields,
        "correct_fields": correct_fields,
        "n_applicable_cases": n_applicable_cases,
        "n_excluded_not_applicable": n_excluded_not_applicable,
        "n_excluded_tool_mismatch": n_excluded_tool_mismatch,
        "n_cases": len(results),
    }


def slot_extraction_accuracy(results: list[dict]) -> float:
    """Fraction of expected argument fields extracted correctly, over cases where the expected tool was
    called.

    Cases with no expected tool or no expected args are skipped, as are cases where the expected tool
    was never called (counted by `tool_selection_accuracy`). Read this together with
    `slot_extraction_stats(...)["n_applicable_cases"]`: if the agent called a different tool in many
    cases, a high value covers only the tool it did call.

    Each key in `expected["args"]` adds one to the denominator, and one to the numerator when the same
    key (case-insensitive) is in the matching call and the normalized values match (see
    `_values_match`).
    """
    return slot_extraction_stats(results)["accuracy"]


_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s?([kKmM])?")
_ZERO_RESULT_PHRASES = (
    "don't have any",
    "do not have any",
    "don't see any",
    "no matching",
    "no results",
    "no vehicles",
    "no listings",
    "no properties",
    "couldn't find any",
    "could not find any",
    "none available",
    "no suitable options",
    "came back empty",
    "no actual",
)


_SUGGESTION_KEYWORDS = (
    "refine",
    "narrow",
    "would you like",
    "expand your search",
    "widen",
    "ballpark",
    "in mind",
)


def _parse_money(text: str, skip_suggestions: bool = True) -> set[float]:
    """Extract dollar amounts from free text, as floats.

    Handles full figures ("$28,510") and "k" and "m" shorthand ("$250k" is 250000.0, "$1.279M" is
    1279000.0), which agents use when restating a price.

    With `skip_suggestions` (the default, for grounding checks), figures in a sentence containing a
    suggestion keyword ("Would you like me to refine the search to $800,000-$1,000,000?") are dropped:
    they are proposed next steps, not claims about the results.
    """
    amounts = set()
    sentences = re.split(r"(?<=[.!?])\s+", text) if skip_suggestions else [text]
    for sentence in sentences:
        if skip_suggestions and any(kw in sentence.lower() for kw in _SUGGESTION_KEYWORDS):
            continue
        for match in _MONEY_RE.finditer(sentence):
            raw = match.group(1).replace(",", "")
            suffix = match.group(2)
            try:
                value = float(raw)
            except ValueError:
                continue
            if suffix and suffix.lower() == "k":
                value *= 1_000
            elif suffix and suffix.lower() == "m":
                value *= 1_000_000
            amounts.add(value)
    return amounts


def _grounded_prices(tool_results: list[dict], price_max: float | None, message: str) -> set[float]:
    """Prices a reply may legitimately cite: each match's price, the `price_max` ceiling, and any dollar
    figure in the customer's own message.

    Agents often restate the customer's ceiling or compare against a number they gave, which is not a
    fabrication.
    """
    grounded: set[float] = set()
    for tool_result in tool_results:
        for match in tool_result.get("matches", []):
            price = match.get("price")
            if isinstance(price, (int, float)):
                grounded.add(round(float(price), 2))
    if price_max is not None:
        try:
            grounded.add(round(float(price_max), 2))
        except (TypeError, ValueError):
            pass
    grounded |= _parse_money(message)
    return grounded


def _is_grounded(amount: float, grounded: set[float], rel_tol: float = 0.06) -> bool:
    """True if `amount` equals a grounded price or is within `rel_tol` of one.

    Agents round real prices ("well above $900k" for a $949,900 listing). The 6% default is the
    smallest round margin that covers the largest legitimate rounding in the 93-case dataset (5.25%). A looser margin would let a near-miss fabrication, such as $1,050,000 against a
    real $949,900, count as grounded.
    """
    for g in grounded:
        if g == 0:
            if amount == 0:
                return True
            continue
        if abs(amount - g) / g <= rel_tol:
            return True
    return False


def _mentions_zero_results(reply: str) -> bool:
    lowered = reply.lower()
    return any(phrase in lowered for phrase in _ZERO_RESULT_PHRASES)


def _reply_cites_real_matches(reply: str, search_tool_results: list[dict]) -> bool:
    """True if the reply engages with the matches it got (a real city, state, location, make or model).

    This separates a literal claim of emptiness ("we have no results") from a true statement about a
    subset ("none of these are townhouses") that happens to use "no" or "none" language.
    """
    lowered = reply.lower()
    for tool_result in search_tool_results:
        for match in tool_result.get("matches", []):
            attrs_raw = match.get("attributes")
            if isinstance(attrs_raw, str):
                try:
                    attrs = json.loads(attrs_raw)
                except json.JSONDecodeError:
                    attrs = {}
            else:
                attrs = attrs_raw or {}
            for key in ("city", "state", "location", "make", "model"):
                value = attrs.get(key)
                if value and str(value).lower() in lowered:
                    return True
    return False


def hallucination_rate(results: list[dict]) -> float:
    """Fraction of applicable cases whose reply makes a claim not grounded in the tool results.

    Applicable: a search tool (search_inventory or search_listings) was called, with or without matches.
    `create_lead` and no-tool cases are excluded from the denominator.

    A case is flagged when either check fails:

    1. Price grounding: every dollar amount in the reply must equal a real match price, the `price_max`
       ceiling, or a figure from the customer's message, within the tolerance in `_is_grounded`.
    2. Zero-result honesty: a reply using "no results" language must not contradict tool results that
       returned matches, unless it engages with those matches (see `_reply_cites_real_matches`).

    This is a lightweight regex and substring check, not an NLP pipeline. It will not catch invented
    names without a price, mileage or location errors, or a "no matches" claim that is true for only
    one sub-query of a multi-call turn.
    """
    return len(hallucination_details(results)) / len(_applicable_cases(results)) if _applicable_cases(results) else 0.0


def _applicable_cases(results: list[dict]) -> list[dict]:
    applicable = []
    for r in results:
        actual = r["actual"]
        tool_results = actual.get("tool_results") or []
        calls = actual.get("tool_calls_made") or []
        search_tool_results = [
            tr
            for call, tr in zip(calls, tool_results)
            if call.get("name") in ("search_inventory", "search_listings")
        ]
        if search_tool_results:
            applicable.append(r)
    return applicable


def hallucination_details(results: list[dict]) -> list[dict]:
    """Same logic as `hallucination_rate`, but returns the flagged cases with reasons, for manual auditing.

    Each entry carries the case's `index` in `results`, so callers such as `task_completion_rate` can
    identify cases by position, since `message` is not guaranteed to be unique.
    """
    details = []
    for index, r in enumerate(results):
        actual = r["actual"]
        tool_results = actual.get("tool_results") or []
        calls = actual.get("tool_calls_made") or []
        search_tool_results = [
            tr
            for call, tr in zip(calls, tool_results)
            if call.get("name") in ("search_inventory", "search_listings")
        ]
        if not search_tool_results:
            continue

        reply = actual.get("reply", "")
        price_max = r["expected"].get("args", {}).get("price_max")
        grounded = _grounded_prices(search_tool_results, price_max, r["message"])
        mentioned = _parse_money(reply)
        ungrounded_prices = sorted(m for m in mentioned if not _is_grounded(m, grounded))

        total_matches = sum(len(tr.get("matches", [])) for tr in search_tool_results)
        claims_zero = _mentions_zero_results(reply)
        engages_with_real_matches = _reply_cites_real_matches(reply, search_tool_results)
        false_zero_claim = claims_zero and total_matches > 0 and not engages_with_real_matches

        if ungrounded_prices or false_zero_claim:
            details.append(
                {
                    "index": index,
                    "message": r["message"],
                    "reply": reply,
                    "ungrounded_prices": ungrounded_prices,
                    "grounded_prices": sorted(grounded),
                    "false_zero_claim": false_zero_claim,
                    "total_matches": total_matches,
                }
            )
    return details



def task_completion_rate(results: list[dict]) -> float:
    """Fraction of cases completed end to end. This is the strictest and most subjective metric.

    A case counts only if all four hold:

    1. The first tool called matches `expected["tool"]` (or none was expected and none was called).
    2. Every expected argument was extracted correctly (the same `_case_slot_fields` check as
       `slot_extraction_accuracy`). Cases with nothing to check pass this condition.
    3. The reply is non-trivial: at least five characters after stripping whitespace.
    4. The case is not flagged by `hallucination_details` (matched by index, not by message).

    It is all-or-nothing on purpose: from the user's side, a task is either served correctly or it is not.
    """
    if not results:
        return 0.0

    flagged_indices = {d["index"] for d in hallucination_details(results)}

    completed = 0
    for index, r in enumerate(results):
        expected = r["expected"]
        expected_tool = expected.get("tool")
        actual = r["actual"]
        actual_tool = _first_tool_name(actual)

        if expected_tool != actual_tool:
            continue

        fields = _case_slot_fields(r)
        if fields is not None and not all(fields):
            continue

        reply = actual.get("reply", "") or ""
        if len(reply.strip()) < 5:
            continue

        if index in flagged_indices:
            continue

        completed += 1

    return completed / len(results)



def mean_latency_ms(results: list[dict]) -> float:
    """Mean of the recorded `latency_ms` field across all cases."""
    if not results:
        return 0.0
    latencies = [r["latency_ms"] for r in results if "latency_ms" in r]
    return sum(latencies) / len(latencies) if latencies else 0.0



def summarize(name: str, results: list[dict]) -> dict:
    slot_stats = slot_extraction_stats(results)
    return {
        "domain": name,
        "n_cases": len(results),
        "tool_selection_accuracy": tool_selection_accuracy(results),
        "slot_extraction_accuracy": slot_stats["accuracy"],
        "slot_n_applicable": slot_stats["n_applicable_cases"],
        "hallucination_rate": hallucination_rate(results),
        "task_completion_rate": task_completion_rate(results),
        "mean_latency_ms": mean_latency_ms(results),
    }


def print_table(summaries: list[dict]) -> None:
    headers = [
        "domain",
        "n_cases",
        "tool_selection_accuracy",
        "slot_extraction_accuracy",
        "slot_n_applicable",
        "hallucination_rate",
        "task_completion_rate",
        "mean_latency_ms",
    ]
    col_widths = {h: max(len(h), 10) for h in headers}
    for s in summaries:
        for h in headers:
            col_widths[h] = max(col_widths[h], len(f"{s[h]:.4f}" if isinstance(s[h], float) else str(s[h])))

    def fmt_row(values: list[str]) -> str:
        return " | ".join(v.ljust(col_widths[h]) for h, v in zip(headers, values))

    print(fmt_row(headers))
    print("-+-".join("-" * col_widths[h] for h in headers))
    for s in summaries:
        row = []
        for h in headers:
            v = s[h]
            row.append(f"{v:.4f}" if isinstance(v, float) else str(v))
        print(fmt_row(row))


def main() -> None:
    base = Path(__file__).parent / "results"
    domains = {
        "cars": base / "cars_results.json",
        "real_estate": base / "real_estate_results.json",
    }

    summaries = []
    for name, path in domains.items():
        results = load_results(path)
        summaries.append(summarize(name, results))

    print_table(summaries)

    print("\n--- Slot-extraction applicability breakdown ---")
    for name, path in domains.items():
        results = load_results(path)
        stats = slot_extraction_stats(results)
        print(
            f"{name}: {stats['accuracy']:.4f} accuracy over {stats['total_fields']} fields "
            f"in {stats['n_applicable_cases']}/{stats['n_cases']} applicable cases "
            f"({stats['n_excluded_not_applicable']} excluded: no tool/args expected; "
            f"{stats['n_excluded_tool_mismatch']} excluded: expected tool never actually called)"
        )

    print("\n--- Hallucination audit (flagged cases) ---")
    for name, path in domains.items():
        results = load_results(path)
        details = hallucination_details(results)
        print(f"\n{name}: {len(details)} flagged case(s)")
        for d in details[:5]:
            print(f"  message: {d['message']}")
            print(f"    ungrounded_prices: {d['ungrounded_prices']}")
            print(f"    false_zero_claim: {d['false_zero_claim']}, total_matches: {d['total_matches']}")


if __name__ == "__main__":
    main()
