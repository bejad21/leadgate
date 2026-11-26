"""Claim-level grounding: pull each checkable claim out of a reply and check it against what the tools returned.

Claims it extracts: dollar amounts, result counts, mileage, bedrooms, bathrooms, floor size, model years,
"City, ST" places and "nothing was found". Each is checked against the rows the search tools returned
(plus the catalog rows a no-tools baseline was shown), against numbers the customer wrote, and against
what the agent itself searched for. It is deterministic: regular expressions and set membership, no model.

What it cannot see: a claim it has no pattern for ("a great family car", a made-up feature), a place
written without a state, a number written out in words other than one to ten. The report says so, and
`sample_for_audit` produces a sheet of replies for the owner to hand-check against these verdicts.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
from typing import Any

from eval import detectors
from eval.rules import ZERO_RESULT_PHRASES

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_STATES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois "
    "indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri "
    "montana nebraska nevada new hampshire jersey mexico york north carolina dakota ohio oklahoma oregon "
    "pennsylvania rhode island south tennessee texas utah vermont virginia washington west wisconsin wyoming "
    "district of columbia puerto rico"
).split()
_STATE_WORDS = set(_STATES)
_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA "
    "RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)

_COUNT_NOUN = r"(?:results?|listings?|homes?|houses?|properties|options|cars?|vehicles?|matches|choices|units)"
_COUNT_RE = re.compile(
    rf"\b(\d[\d,]*|{'|'.join(_NUMBER_WORDS)})\s+(?:matching\s+|available\s+|different\s+|total\s+)?{_COUNT_NOUN}\b", re.IGNORECASE
)
_THOUSANDS = r"\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+"  # 126,827 and 126 827 and 126.827
_MILEAGE_RE = re.compile(rf"(?<![\d.,])({_THOUSANDS}|\d+)\s*(k)?\s*(?:miles|mi)\b", re.IGNORECASE)
_BED_RE = re.compile(r"\b(\d+)\s*[- ]?(?:bd|beds?|bedrooms?)\b", re.IGNORECASE)
_BATH_RE = re.compile(r"\b(\d+(?:\.\d)?)\s*[- ]?(?:ba|baths?|bathrooms?)\b", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{3,})\s*(?:sq\.?\s?ft|sqft|square (?:feet|foot))", re.IGNORECASE
)  # "1,320", "1.320" (German thousands) and "1320"
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RE = re.compile(  # a calendar date, whose year is not a model year
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d\d\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\s+(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_HEDGE_BEFORE_COUNT = re.compile(r"(?:over|more than|at least|upwards of)\s*$", re.IGNORECASE)
# A town word, or an abbreviation that is part of a town's name ("Ft. Thomas", "Port St. Lucie").
_TOWN_WORD = r"(?:(?:Ft|St|Mt)\.|[A-Z][a-z]+)"
_PLACE_RE = re.compile(rf"\b({_TOWN_WORD}(?:[ -]{_TOWN_WORD})*),\s*([A-Z]{{2}}\b|[A-Z][a-z]+(?: [A-Z][a-z]+)?)")


def _rows(tool_results: list, evidence_rows: list | None) -> list[dict]:
    rows = []
    for result in tool_results or []:
        if isinstance(result, dict):
            rows += [m for m in result.get("matches") or [] if isinstance(m, dict)]
    rows += [m for m in evidence_rows or [] if isinstance(m, dict)]
    return rows


def _attrs(row: dict) -> dict:
    raw = row.get("attributes")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _numbers_in(text: str) -> list[float]:
    """Numbers written in `text`, with "k" and "m" shorthand expanded and commas read as thousands."""
    out: list[float] = []
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(?:(grand|thousand|million|mil)(?![a-z])|([kKmM])(?![A-Za-z]))?", text or "", re.IGNORECASE):
        value = _num(match.group(1))
        if value is None:
            continue
        out.append(value)
        unit = (match.group(2) or match.group(3) or "").lower()
        if unit:
            out.append(value * (1_000 if unit in ("k", "grand", "thousand") else 1_000_000))
    return out


class _Evidence:
    def __init__(self, rows: list[dict], tool_results: list, tool_calls: list, context_text: str):
        self.rows = rows
        self.prices = [float(r["price"]) for r in rows if isinstance(r.get("price"), (int, float))]
        self.mileages, self.years, self.beds, self.baths, self.sizes = [], set(), set(), set(), []
        self.cities: set[str] = set()
        for row in rows:
            attrs = _attrs(row)
            for value in (_num(attrs.get("mileage")),):
                if value is not None:
                    self.mileages.append(value)
            self.years |= {int(y) for y in _YEAR_RE.findall(f"{row.get('name', '')} {attrs.get('year', '')}")}
            name_beds = re.search(r"(\d+)bd/(\d+(?:\.\d)?)ba", str(row.get("name", "")))
            for key, pool, group in (("bed", self.beds, 1), ("bath", self.baths, 2)):
                value = _num(attrs.get(key)) if attrs.get(key) is not None else (_num(name_beds.group(group)) if name_beds else None)
                if value is not None:
                    pool.add(value)
            if _num(attrs.get("house_size")) is not None:
                self.sizes.append(_num(attrs.get("house_size")))
            for key in ("city",):
                if attrs.get(key):
                    self.cities.add(str(attrs[key]).lower())
            if attrs.get("location"):
                self.cities.add(str(attrs["location"]).split(",")[0].strip().lower())
        self.counts: set[float] = set()
        for result in tool_results or []:
            if isinstance(result, dict) and "matches" in result:
                self.counts.add(float(result.get("count", len(result["matches"]))))
                self.counts.add(float(len(result["matches"])))
        # Cars a search reported as on hold or sold are named in the result; their name and year are evidence for what a
        # reply says about them, but they are not matches, so they do not count as rows.
        for result in tool_results or []:
            for held in (result.get("unavailable") if isinstance(result, dict) else None) or []:
                if isinstance(held, dict):
                    self.years |= {int(y) for y in _YEAR_RE.findall(str(held.get("name", "")))}
        self.n_search_matches = sum(
            len(r.get("matches") or []) for r in tool_results or [] if isinstance(r, dict)
        )
        self.context = [*_numbers_in(context_text)]
        self.context_text = (context_text or "").lower()
        for call in tool_calls or []:
            for value in (call.get("arguments") or {}).values():
                number = _num(value)
                if number is not None:
                    self.context.append(number)
        self.years |= {int(y) for y in _YEAR_RE.findall(context_text or "")}

    def has(self, value: float, pool, tolerance: float = 0.5) -> bool:
        return any(abs(value - p) <= tolerance for p in list(pool) + self.context)


_EXAMPLE_CUE = re.compile(r"\b(?:for example|for instance|e\.g\.|such as)\b", re.IGNORECASE)


def _price_claims(reply: str) -> list[tuple[float, float]]:
    """The dollar amounts a reply states, in order, except round amounts offered as examples inside a question that says
    "for example" ("what is your budget, for example around $500k?"). The exception is per sentence: a round price stated
    in another sentence is still checked, and so is a made-up price like $21,837 in the same question."""
    claims: list[tuple[float, float]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", reply):
        example = "?" in sentence and bool(_EXAMPLE_CUE.search(sentence))
        for value, tolerance in detectors.extract_price_claims(sentence):
            if not (example and value % 1000 == 0):
                claims.append((value, tolerance))
    return claims


def _claim(kind: str, text: str, value: Any, grounded: bool, why: str = "") -> dict:
    return {"type": kind, "text": text, "value": value, "grounded": grounded, "why": "" if grounded else why}


def _cites_real_matches(reply: str, rows: list[dict]) -> bool:
    lowered = reply.lower()
    for row in rows:
        attrs = _attrs(row)
        for key in ("city", "state", "location", "make", "model"):
            if attrs.get(key) and str(attrs[key]).lower() in lowered:
                return True
    return False


def check_reply(
    reply: str,
    *,
    tool_results: list,
    tool_calls: list,
    context_text: str = "",
    evidence_rows: list[dict] | None = None,
) -> list[dict]:
    """One verdict per checkable claim in `reply`: {type, text, value, grounded, why}."""
    reply = detectors.normalize(reply or "")
    ev = _Evidence(_rows(tool_results, evidence_rows), tool_results, tool_calls, context_text)
    verdicts: list[dict] = []
    masked = reply

    for value, tolerance in _price_claims(reply):
        ok = ev.has(value, ev.prices, tolerance)
        verdicts.append(_claim("price", f"${value:,.0f}", value, ok, f"{value:,.0f} is not a returned price or a figure the customer gave"))
    masked = re.sub(r"[$][\s\d,.]*[\d][kKmM]?|\bUSD\s*[\d,.]+|\b\d[\d,]*\s*(?:dollars?|usd|bucks)\b", " ", masked, flags=re.IGNORECASE)

    for match in _COUNT_RE.finditer(masked):
        raw = match.group(1).lower()
        value = float(_NUMBER_WORDS[raw]) if raw in _NUMBER_WORDS else float(raw.replace(",", ""))
        if value >= 1900 and raw not in _NUMBER_WORDS:
            continue  # a model year, not a count
        if _HEDGE_BEFORE_COUNT.search(masked[max(0, match.start() - 14) : match.start()]):
            ok = any(c >= value for c in ev.counts)  # "over 300" is true when the real count is higher
        else:
            ok = ev.has(value, ev.counts, 0.0)
        verdicts.append(_claim("count", match.group(0), value, ok, f"{value:g} results were not returned"))

    for match in _MILEAGE_RE.finditer(masked):
        value = float(re.sub(r"[ \u00a0\u202f.,]", "", match.group(1))) * (1000 if match.group(2) else 1)
        tolerance = 500 if match.group(2) else 0.5
        verdicts.append(_claim("mileage", match.group(0), value, ev.has(value, ev.mileages, tolerance), f"no returned car has {value:,.0f} miles"))
    for match in _SIZE_RE.finditer(masked):
        value = float(re.sub(r"[.,]", "", match.group(1)))
        verdicts.append(_claim("size", match.group(0), value, ev.has(value, ev.sizes), f"no returned home is {value:,.0f} sq ft"))
    for match in _BED_RE.finditer(masked):
        value = float(match.group(1))
        verdicts.append(_claim("bedrooms", match.group(0), value, ev.has(value, ev.beds, 0.0), f"no returned home has {value:g} bedrooms"))
    for match in _BATH_RE.finditer(masked):
        value = float(match.group(1))
        verdicts.append(_claim("bathrooms", match.group(0), value, ev.has(value, ev.baths, 0.0), f"no returned home has {value:g} bathrooms"))

    stripped = _DATE_RE.sub(" ", _SIZE_RE.sub(" ", _MILEAGE_RE.sub(" ", masked)))
    for match in _YEAR_RE.finditer(stripped):
        year = int(match.group(1))
        verdicts.append(_claim("year", match.group(1), year, year in ev.years or year in ev.context, f"no returned car is from {year}"))

    for match in _PLACE_RE.finditer(reply):
        state = match.group(2)
        if state not in _STATE_CODES and not all(w.lower() in _STATE_WORDS for w in state.split()):
            continue
        words = re.split(r"[ -]", match.group(1))
        suffixes = [" ".join(words[i:]).lower() for i in range(len(words))]
        ok = any(s in ev.cities or s in ev.context_text for s in suffixes)
        verdicts.append(_claim("location", match.group(0), match.group(1), ok, f"{match.group(1)} is not in the returned listings"))

    lowered = reply.lower().replace("’", "'")
    searched = any(isinstance(r, dict) and "matches" in r for r in tool_results or [])
    if searched and any(p in lowered for p in ZERO_RESULT_PHRASES):
        rows = _rows(tool_results, None)
        # "Nothing at 20 million" is true when the search with that filter came back empty, even if the agent then looked at
        # the whole catalog to offer something else.
        some_empty = any(isinstance(r, dict) and "matches" in r and not r["matches"] for r in tool_results or [])
        ok = not rows or some_empty or _cites_real_matches(reply, rows)
        verdicts.append(_claim("none_found", "says nothing was found", None, ok, f"{len(rows)} matches were returned"))
    return verdicts


def sample_for_audit(records: list[dict], n: int, seed: int) -> list[dict]:
    """`n` randomly chosen records that have a reply, the same for the same seed. Fewer if fewer exist."""
    pool = [r for r in records if (r.get("actual") or {}).get("reply")]
    return random.Random(seed).sample(pool, min(n, len(pool)))


def audit_sheet(records: list[dict]) -> str:
    """CSV for the owner to hand-check: id, reply, the claims found, the automatic verdict, and empty columns to fill in."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["case_id", "repeat", "reply", "claims", "verdict", "owner_verdict", "owner_notes"])
    for record in records:
        found = (record.get("grounding") or {}).get("claims") or []
        bad = [c for c in found if not c["grounded"]]
        verdict = "no claims found" if not found else ("all grounded" if not bad else f"{len(bad)} ungrounded: " + "; ".join(c["text"] for c in bad))
        writer.writerow(
            [record.get("case_id"), record.get("repeat"), (record["actual"]["reply"] or "").replace("\n", " "),
             "; ".join(f"{c['type']}={c['text']}" for c in found), verdict, "", ""]
        )
    return out.getvalue()
