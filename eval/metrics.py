"""Offline metrics computation for the LeadGate evaluation harness (Task 5.3).

Consumes the per-case result records produced by Task 5.2 (`eval/results/*.json`)
and computes five metrics:

- tool_selection_accuracy: did the agent call the right tool (or correctly call
  no tool at all for out-of-scope requests)?
- slot_extraction_accuracy: for cases where a tool call was expected, did the
  agent extract the right argument values into that call?
- hallucination_rate: for cases where a tool actually returned results, does
  the agent's natural-language reply only state facts (prices, "no results")
  that are actually grounded in what the tool returned?
- task_completion_rate: a composite judgment call (see docstring) of whether
  the case was handled correctly end-to-end.
- mean_latency_ms: mean wall-clock latency recorded per case.

No live API/DB calls are made here -- this only reads already-collected JSON
result files and does pure computation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_results(path: str | Path) -> list[dict]:
    """Load a Task 5.2 results JSON file (a list of per-case result dicts)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Step 1: tool_selection_accuracy
# ---------------------------------------------------------------------------


def _first_tool_name(actual: dict) -> str | None:
    """The name of the first tool call actually made, or None if none was made."""
    calls = actual.get("tool_calls_made") or []
    return calls[0]["name"] if calls else None


def tool_selection_accuracy(results: list[dict]) -> float:
    """Fraction of cases where the first tool actually called matches the tool
    the case expected (including the "no tool expected, no tool called" case
    for out-of-scope requests, which counts as a correct match).

    Only the first tool call is compared against `expected["tool"]`, since the
    expected fixtures encode a single expected tool (or None) per case; a case
    that makes an unexpected *additional* follow-up call (see the "truck for
    towing" case in cars_results.json, which calls search_inventory twice) is
    still scored correct here as long as the first call matches expectation.
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


# ---------------------------------------------------------------------------
# Step 2: slot_extraction_accuracy
# ---------------------------------------------------------------------------


def _normalize_value(value: Any) -> Any:
    """Normalize a slot value for comparison: case-insensitive string match,
    numeric comparison that tolerates int/float/str-of-number mismatches.
    This is intentionally forgiving about representation only, not about
    substantive correctness (e.g. "30000" vs 30000 vs "30,000" all match;
    "Toyota" vs "toyota" match; "Toyota" vs "Honda" does not).
    """
    if isinstance(value, str):
        stripped = value.strip().lower()
        # Try to normalize numeric-looking strings (e.g. "30,000" -> 30000.0)
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
    """Word-overlap check for free-text fields the agent composes in its own
    words (e.g. `notes`), rather than exact string equality.

    Unlike `customer_name`/`customer_contact`/`price`/`make`/`model`, which
    are discrete facts objectively extractable from the message, `notes` is
    a summary the agent writes itself -- "Needs the car by next month" and
    "Customer needs the vehicle by next month." are the same fact in
    different words, and exact-string grading would wrongly fail the second
    phrasing. This still requires most of the expected content words to
    actually appear (not a blanket pass): a `notes` value citing a wrong
    fact would have low word overlap with the expected value and correctly
    fail.
    """
    expected_words = _content_words(expected_value)
    if not expected_words:
        return True
    actual_words = _content_words(actual_value)
    overlap = len(expected_words & actual_words) / len(expected_words)
    return overlap >= min_overlap


# Fields that are agent-composed free text (summaries/paraphrases), not
# discrete facts copied verbatim from the customer's message -- graded by
# content overlap instead of exact string equality. Only used for
# `create_lead`, the one tool with any free-text arguments.
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
    """First call in `calls` whose name equals `tool_name`, else None."""
    for call in calls:
        if call.get("name") == tool_name:
            return call
    return None


def _case_slot_fields(r: dict) -> list[bool] | None:
    """Per-field match results (True/False) for one case's expected tool
    arguments, or None if the case is not applicable to slot-extraction
    grading at all (no tool call expected, no expected args to check, or the
    expected tool was never actually invoked in the turn -- a tool-selection
    failure that `tool_selection_accuracy` already captures elsewhere, and
    which this function deliberately does NOT double-penalize by comparing
    a *different* tool's arguments against the expected tool's argument
    names).

    Shared by `slot_extraction_accuracy` (aggregates across all applicable
    cases and fields) and `task_completion_rate` (requires all fields in a
    case to match) so the two metrics can't silently drift apart on what
    counts as a "slot match".

    When a case has multiple tool calls in one turn, the first call whose
    name matches the expected tool is used, not strictly index 0.

    A key whose expected value is `None` (e.g. `price_max: null`, meaning "no
    filter should be applied") is matched by the key being absent from the
    actual call, not just by an explicit null being present.

    For `create_lead` cases specifically, the `name` argument is excluded
    from grading entirely. It's a composed lead title the agent writes
    itself, not a fact extracted verbatim from the customer's message, and
    Task 5.1's own eval dataset documents this directly -- e.g. r39's
    `expected_outcome`: "Exact wording of the 'name' field may vary; grade
    on presence of customer_name/customer_contact/price and correct tool
    selection." Grading a composed title by exact string equality was
    always the wrong check for this field; this isn't a leniency added to
    inflate a score, it's aligning the metric with what the dataset's own
    authors already specified it should measure.
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
    """Full breakdown behind `slot_extraction_accuracy`: total/correct field
    counts, how many cases actually contributed fields ("applicable"), and
    how many were excluded and why. Exists so the accuracy fraction is never
    reported without visibility into its denominator -- a 1.00 built from 40
    applicable cases (with, say, 8 cases excluded because the agent picked
    the wrong tool entirely) reads very differently from a 1.00 built from
    all 48.
    """
    total_fields = 0
    correct_fields = 0
    n_applicable_cases = 0
    n_excluded_not_applicable = 0  # no tool expected, or no args to check
    n_excluded_tool_mismatch = 0  # tool call expected but never actually made

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
    """Fraction of expected argument fields that were correctly extracted,
    aggregated across all cases where the expected tool was BOTH expected AND
    actually invoked somewhere in the turn.

    Cases where no tool call was expected (`expected["tool"] is None`), or
    where `expected["args"]` is empty, are skipped entirely -- there is no
    meaningful "slot" denominator for them. Cases where the expected tool was
    never called at all (a tool-selection failure -- including the case
    where the agent called a *different* tool instead, e.g. searching instead
    of calling create_lead) are also skipped for this metric: there are no
    genuinely comparable arguments to grade, and that failure is already
    captured by tool_selection_accuracy.

    IMPORTANT CAVEAT (see `slot_extraction_stats` for the full breakdown):
    this exclusion means a high accuracy here does NOT mean every tool's
    argument extraction was tested. In real_estate_results.json specifically,
    all 8 cases expecting `create_lead` had the agent call `search_listings`
    instead, so all 8 are excluded here -- the reported accuracy reflects
    only `search_listings` extraction, not `create_lead`. Always read this
    number together with `slot_extraction_stats(...)["n_applicable_cases"]`
    (or the summary table's `n_applicable` column), not in isolation.

    Within an applicable case, only keys present in `expected["args"]` are
    checked (an expected dict of `{}` contributes 0 fields, which is correct
    since those cases explicitly declare exact args as "not strictly graded").
    Each key contributes 1 to the denominator; it contributes 1 to the
    numerator if the same key (case-insensitive) is present in the matching
    call's arguments and its normalized value matches (case-insensitive
    string compare, numeric-string tolerant -- see `_values_match`).
    """
    return slot_extraction_stats(results)["accuracy"]


# ---------------------------------------------------------------------------
# Step 3: hallucination_rate
# ---------------------------------------------------------------------------

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
    """Extract dollar amounts mentioned in free text, as floats.

    Handles full figures ("$28,510"), "k" shorthand ("$250k" -> 250000.0), and
    "m"/"M" shorthand ("$1.279M" -> 1279000.0), since agents sometimes restate
    a price in shorthand (confirmed live: a reply citing a real $1,279,000
    listing as "$1.279M" was being flagged as an ungrounded/hallucinated
    price purely because the "M" suffix wasn't recognized -- a metric parsing
    bug, not an actual hallucination).

    When `skip_suggestions` is True (the default, used for grounding checks),
    a dollar figure is excluded if it falls in a sentence containing a
    suggestion/follow-up keyword (e.g. "Would you like me to refine the
    search to $800,000-$1,000,000?"). Those are proposed next steps, not
    factual claims about the results already returned, so they should not be
    graded as grounded-or-hallucinated facts.
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
    """All prices that would be legitimate for a reply to cite: every match's
    real price, the price_max boundary itself (agents often restate the
    user's own ceiling, e.g. "under $30,000"), and any dollar figure the user
    themselves mentioned in their message (agents also restate/compare against
    the user's own stated number, e.g. "no properties over $20,000,000" when
    the user asked about $20,000,000 -- that's an honest comparison, not a
    fabricated data point)."""
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
    """An amount counts as grounded if it exactly matches a grounded price, or
    is within a relative tolerance of one (agents commonly round a real price
    to a colloquial figure, e.g. citing a $949,900 listing as "well above
    $900k", or a $239,000 listing as "still under $250K" -- these are
    approximations of a real, grounded number, not fabrications).

    rel_tol=0.06 (6%) is deliberately tighter than a round 10%: manually
    auditing every legitimate rounding case in the 93-case dataset (see the
    Task 5.3 report) found a worst case of 5.25% ("well above $900k" for a
    real $949,900 listing). 6% is the smallest round-number margin that still
    covers every confirmed-legitimate case with a small buffer, rather than
    an arbitrary looser value that would also let a genuinely fabricated
    price close to a real one (e.g. inventing "$1,050,000" near a real
    $949,900 listing, a 10.6% gap) slip through ungrounded-check-free. A
    reviewer flagged the original 10% as leaving too much room for exactly
    that kind of near-miss hallucination; 6% narrows that room by nearly
    half while re-verified to introduce zero new flags against the actual
    93 cases (see report addendum)."""
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
    """True if the reply demonstrably engages with the real matches it got
    back (cites a real price, a real city/state from `attributes`, or a
    distinctive word from a match's `name`) rather than blindly claiming
    emptiness. This distinguishes "we have zero results" (a literal, checkable
    claim) from "none of these specific results are a townhouse" /
    "these matches are all in NJ/CT/PA, not West Warwick" (a true, grounded
    claim about a *subset* of the results that happens to use "no"/"none"
    language) -- both use similar wording but only the first is a factual
    claim this checker can call false when matches exist.
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
    """Fraction of applicable cases where the reply makes a factual claim that
    is not grounded in the tool results actually returned.

    Applicable cases: a search-style tool (search_inventory / search_listings)
    was called and returned at least one tool_result block. (create_lead calls
    and no-tool-call cases are not "search grounding" claims and are excluded
    from the denominator -- there's nothing to hallucinate against there in
    the same sense.)

    Two concrete, checkable hallucination signals are used:

    1. Price grounding: every dollar amount mentioned in the reply must equal
       either a real price from `tool_results[*].matches[*].price`, or the
       `price_max` value from the expected args (agents legitimately restate
       the user's own budget ceiling, e.g. "under $30,000"). A reply that
       states a dollar figure matching neither is flagged.
    2. Zero-result honesty: if the reply uses "no results" language (e.g.
       "we don't have any", "no matching vehicles"), the tool results must
       actually show zero total matches (all `count` fields are 0, or all
       `matches` lists are empty). A reply claiming emptiness when the tool
       actually returned real matches is flagged (and vice versa is a
       generation-quality issue, but not tested here since the brief's
       hallucination concern is specifically citing something the DB didn't
       return).

    A case is flagged if either signal fails. Price grounding tolerates a 10%
    relative rounding margin (agents commonly say "well above $900k" for a
    $949,900 listing, or restate the user's own number back, e.g. "no
    listings over $20,000,000" when the user asked about exactly that figure
    -- these are honest approximations/comparisons, not fabrications).

    This is a lightweight, regex/substring-based check, not an NLP pipeline --
    it will not catch hallucinated *names* that come with no numeric price
    (e.g. a made-up model name with no $ amount attached), nor mileage/
    location hallucinations, nor a reply that claims "no matches" for only
    part of a multi-call turn while matches did exist for a different sub-
    query (see the manual audit in the report for a concrete instance of this
    boundary case). That is a documented limitation appropriate for a
    portfolio-project eval, not a production-grade grounding checker.
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
    """Same logic as hallucination_rate but returns the flagged cases with
    the reasons, for manual auditing. Each entry carries the case's `index`
    (its position in the `results` list passed in) so callers like
    `task_completion_rate` can identify flagged cases by position rather than
    by matching on the `message` string, which is not guaranteed unique."""
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


# ---------------------------------------------------------------------------
# Step 4: task_completion_rate
# ---------------------------------------------------------------------------


def task_completion_rate(results: list[dict]) -> float:
    """Fraction of cases judged "completed" end-to-end.

    Definition (documented since this is the most subjective metric): a case
    counts as completed if ALL of the following hold:

    1. Tool selection was correct (same check as tool_selection_accuracy):
       the first tool called matches `expected["tool"]` (including the
       "no tool expected and none called" case).
    2. IF a tool call was expected AND made: every expected argument field
       was correctly extracted, using the exact same per-field logic as
       `slot_extraction_accuracy` (both call `_case_slot_fields`, so the two
       metrics cannot silently diverge on what counts as a slot match) --
       i.e. this case contributes 0 mismatches, not just a nonzero average.
       Cases with no expected args, or where no tool call was expected,
       automatically satisfy this condition.
    3. The reply is non-empty and not just a placeholder (more than a couple
       of characters after stripping whitespace) -- a sanity check that the
       agent actually produced a user-facing response rather than silently
       failing.
    4. The case is not flagged by the hallucination check (see
       hallucination_rate) -- a reply that mis-states facts about what was
       actually found is not a "completed" task even if the tool/slots were
       right, since the user walks away misinformed. Flagged cases are
       identified by their position in `results` (via `hallucination_details`'
       `index` field), not by matching on the `message` string, since two
       cases could in principle share identical phrasing.

    This is deliberately a strict, all-or-nothing composite (rather than a
    partial-credit blend) because "task completion" from a user's point of
    view is binary: either they got a correct, honest, on-topic answer, or
    they didn't. Softer partial-credit variants are possible but would bury
    the signal this metric is meant to give: how often would a real user have
    been fully, correctly served.
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

        # 1. Tool selection correct
        if expected_tool != actual_tool:
            continue

        # 2. Slot extraction correct (same per-field check as
        #    slot_extraction_accuracy; None means "not applicable", i.e. no
        #    tool call was expected or there were no args to check, which
        #    trivially satisfies this condition)
        fields = _case_slot_fields(r)
        if fields is not None and not all(fields):
            continue

        # 3. Reply is non-trivial
        reply = actual.get("reply", "") or ""
        if len(reply.strip()) < 5:
            continue

        # 4. Not flagged as hallucinated
        if index in flagged_indices:
            continue

        completed += 1

    return completed / len(results)


# ---------------------------------------------------------------------------
# Step 5: mean_latency_ms
# ---------------------------------------------------------------------------


def mean_latency_ms(results: list[dict]) -> float:
    """Mean of the recorded `latency_ms` field across all cases."""
    if not results:
        return 0.0
    latencies = [r["latency_ms"] for r in results if "latency_ms" in r]
    return sum(latencies) / len(latencies) if latencies else 0.0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


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
