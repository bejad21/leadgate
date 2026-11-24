"""Load eval datasets, resolve their labels into concrete expectations, rebuild chat history, and validate them.

A stored result carries the resolved expectation (`build_expected`), so scoring needs neither this module's
catalog access nor the datasets.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from eval import resolver, rules
from eval.heldout_rules import HELDOUT_RULES

ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = ROOT / "eval" / "datasets"
SEARCH_TOOLS = ("search_inventory", "search_listings")
REQUIRED_KEYS = ("id", "category", "message", "expected_tool", "expected_args", "expected_outcome")

DEV_FILES = {"cars": "cars_test_set.json", "real_estate": "real_estate_test_set.json"}
HELDOUT_FILES = {"cars": "heldout_cars.json", "real_estate": "heldout_real_estate.json"}
FRESH_FILES = {"cars": "fresh_cars.json", "real_estate": "fresh_real_estate.json"}
FRESH2_FILES = {"cars": "fresh2_cars.json", "real_estate": "fresh2_real_estate.json"}
FRESH3_FILES = {"cars": "fresh3_cars.json", "real_estate": "fresh3_real_estate.json"}
FRESH4_FILES = {"cars": "fresh4_cars.json", "real_estate": "fresh4_real_estate.json"}
FRESH5_FILES = {"cars": "fresh5_cars.json", "real_estate": "fresh5_real_estate.json"}
# The sixth fresh set was written once and split before anything was run: a dev part to find and fix problems on, and a test part
# that is run once, on the final code. Both parts are frozen together under one hash file.
FRESH6DEV_FILES = {"cars": "fresh6dev_cars.json", "real_estate": "fresh6dev_real_estate.json"}
FRESH6TEST_FILES = {"cars": "fresh6test_cars.json", "real_estate": "fresh6test_real_estate.json"}
# The seventh fresh set is split the same way.
FRESH7DEV_FILES = {"cars": "fresh7dev_cars.json", "real_estate": "fresh7dev_real_estate.json"}
FRESH7TEST_FILES = {"cars": "fresh7test_cars.json", "real_estate": "fresh7test_real_estate.json"}
HASH_FILES = {
    "heldout": "HELDOUT_SHA256.txt", "fresh": "FRESH_SHA256.txt", "fresh2": "FRESH2_SHA256.txt", "fresh3": "FRESH3_SHA256.txt",
    "fresh4": "FRESH4_SHA256.txt",
    "fresh5": "FRESH5_SHA256.txt",
    "fresh6dev": "FRESH6_SHA256.txt",
    "fresh6test": "FRESH6_SHA256.txt",
    "fresh7dev": "FRESH7_SHA256.txt",
    "fresh7test": "FRESH7_SHA256.txt",
}


class FrozenFileChanged(Exception):
    """A held-out file no longer matches the hash recorded when it was frozen."""


def dataset_path(domain: str, kind: str) -> Path:
    files = {"dev": DEV_FILES, "heldout": HELDOUT_FILES, "fresh": FRESH_FILES, "fresh2": FRESH2_FILES, "fresh3": FRESH3_FILES, "fresh4": FRESH4_FILES, "fresh5": FRESH5_FILES, "fresh6dev": FRESH6DEV_FILES, "fresh6test": FRESH6TEST_FILES, "fresh7dev": FRESH7DEV_FILES, "fresh7test": FRESH7TEST_FILES}[kind]
    return DATASETS_DIR / files[domain]


def load_dataset(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("dataset_info", {}), data["cases"]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_frozen(directory: Path = DATASETS_DIR, hash_file: str = "HELDOUT_SHA256.txt") -> None:
    """Raise FrozenFileChanged unless every file listed in the hash file has its recorded sha256."""
    path = directory / hash_file
    if not path.exists():
        raise FrozenFileChanged(f"{hash_file} is missing: the set is not frozen yet")
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        if sha256_of(directory / name) != digest:
            raise FrozenFileChanged(f"{name} no longer matches its frozen hash")


def _default_rules(case: dict) -> list[dict]:
    out: list[dict] = [{"rule": "reply_nonempty"}]
    if case.get("expected_tool") is None:
        out += [{"rule": "no_tool_called"}, {"rule": "reply_no_link"}]
    if case.get("expected_tool") in SEARCH_TOOLS:
        out.append({"rule": "says_none_when_empty"})
    return out


def _case_rules(case: dict) -> list[dict]:
    if "rules" in case:
        return [{"rule": "reply_nonempty"}, *case["rules"]]
    entry = HELDOUT_RULES.get(case["id"])
    if entry is None:
        return _default_rules(case)
    base: list[dict] = [{"rule": "reply_nonempty"}]
    if case.get("expected_tool") in SEARCH_TOOLS:
        base.append({"rule": "says_none_when_empty"})
    return base + entry.get("rules", [])


def build_expected(case: dict, catalog: list[dict], reference: dt.date) -> dict:
    """The expectation stored with a result: tool, other accepted first tools, concrete arguments (matchers
    allowed), the rules to check, and any placeholder that could not be resolved."""
    entry = HELDOUT_RULES.get(case["id"], {})
    labelled = entry["args"] if "args" in entry else case.get("expected_args")
    args, errors = resolver.resolve_args(labelled, catalog, reference)
    return {
        "tool": case.get("expected_tool"),
        "accept_tools": case.get("accept_tools", entry.get("accept_tools", [])),
        "args": args,
        "outcome": case.get("expected_outcome"),
        "rules": _case_rules(case),
        "resolution_errors": errors,
    }


def _row_places(row: dict) -> list[str]:
    try:
        attrs = json.loads(row.get("attributes") or "{}")
    except ValueError:
        attrs = {}
    places = [str(attrs.get("city", "")), str(attrs.get("location", "")).split(",")[0]]
    return [p.lower() for p in places if p]


def _rows_shown_in(text: str, catalog: list[dict]) -> list[dict]:
    """Catalog rows an earlier assistant message talked about: the rows whose price it states, narrowed by
    place when several rows share a price."""
    lowered = text.lower()
    prices = {float(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", text)}
    shown: list[dict] = []
    for price in sorted(prices):
        candidates = [r for r in catalog if abs(float(r.get("price", 0)) - price) < 0.5]
        if len(candidates) > 1:
            narrowed = [r for r in candidates if any(p in lowered for p in _row_places(r))]
            candidates = narrowed or candidates
        shown += candidates
    return shown


def build_history(history: list[dict], catalog: list[dict], search_tool: str) -> list[dict]:
    """Rebuild a case's prior turns the way the engine keeps them: an assistant message that listed items
    follows a search call and its result, so the item ids it showed are known to the hold and viewing guard."""
    from engine.core.guardrails import sanitize_tool_result

    built: list[dict] = []
    for index, message in enumerate(history):
        if message.get("role") == "assistant":
            rows = _rows_shown_in(message.get("content") or "", catalog)
            if rows:
                call_id = f"call_history_{index}"
                built.append(
                    {"role": "assistant", "content": None,
                     "tool_calls": [{"id": call_id, "type": "function", "function": {"name": search_tool, "arguments": "{}"}}]}
                )
                result = {"matches": [{k: r[k] for k in ("id", "name", "price", "attributes") if k in r} for r in rows], "count": len(rows)}
                built.append({"role": "tool", "tool_call_id": call_id, "name": search_tool, "content": json.dumps(sanitize_tool_result(result))})
        built.append(message)
    return built


def _rule_names(rule: dict):
    yield rule.get("rule")
    for option in rule.get("options", []):
        for inner in option:
            yield from _rule_names(inner)


def validate_cases(items: list[dict], source: str) -> list[str]:
    """Problems found in a list of cases: missing keys, repeated ids, unknown rules, bad accept_tools or history."""
    problems: list[str] = []
    seen: set[str] = set()
    for position, case in enumerate(items):
        label = f"{source}[{position}] {case.get('id', '?')}"
        for key in REQUIRED_KEYS:
            if key not in case:
                problems.append(f"{label}: missing {key}")
        case_id = case.get("id")
        if case_id in seen:
            problems.append(f"{label}: duplicate id {case_id}")
        seen.add(case_id)
        if "accept_tools" in case and not isinstance(case["accept_tools"], list):
            problems.append(f"{label}: accept_tools must be a list")
        for rule in case.get("rules", []):
            for name in _rule_names(rule):
                if name not in rules.RULES:
                    problems.append(f"{label}: unknown rule {name}")
        for message in case.get("history", []):
            if message.get("role") not in ("user", "assistant") or not isinstance(message.get("content"), str):
                problems.append(f"{label}: history needs role user/assistant and text content")
        if not isinstance(case.get("message"), str) or not case.get("message", "x").strip():
            problems.append(f"{label}: message must be non-empty text")
    return problems
