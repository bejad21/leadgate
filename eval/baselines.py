"""Two baselines scored with the same metrics and datasets as the agent, so the table can say what the agent adds.

(a) KeywordRouter: no model. Rules read make, model, price, year, mileage, condition, place and sort order
    from the text, decide between a search, a lead and no tool, call the same adapter tools, and fill a
    template with what the tool returned. Its vocabulary (makes, models, cities) comes from the catalog.
    It cannot hold or book (no way to resolve an item id from words) and does not understand typos,
    other languages or paraphrase; that is the point of a baseline.

(b) No-tools LLM: the same model, one call per case, with a short prompt that contains the whole catalog
    (one compact line per available item) and no tool definitions. It answers from the prompt alone, so it
    can never create a lead or hold anything. Its replies are checked against the catalog it was shown.
"""

from __future__ import annotations

import json
import re
import time

from engine.core.tools import parse_tool_args
from eval import cases

ROUTER_MODEL = "keyword-router"
_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()


STATES = dict(zip(
    ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
     "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
     "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
     "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
     "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
     "wisconsin", "wyoming"],
    _STATE_CODES, strict=True,
))

_SUFFIX = r"(?:k|thousand|million|mil|m)\b"
_AMOUNT = rf"(?:\$\s*)?(?P<n>\d[\d,]*(?:\.\d+)?|a)\s*(?P<s>{_SUFFIX})?"
_CEILING = re.compile(rf"(?:under|below|less than|max(?:imum)?|up to|no more than|cheaper than|at most|within|budget of)\s*{_AMOUNT}")
_FLOOR = re.compile(rf"(?:over|above|more than|at least|minimum)\s*{_AMOUNT}")
_BETWEEN = re.compile(rf"between\s*(?:\$\s*)?(?P<a>\d[\d,]*(?:\.\d+)?)\s*(?P<as>{_SUFFIX})?\s*(?:and|to|-)\s*{_AMOUNT}")
_MILEAGE = re.compile(r"(?:under|less than|below|fewer than|max(?:imum)?|up to)\s*(\d[\d,]*)\s*(k)?\s*miles?")
_YEAR_MIN = re.compile(r"(\d{4})\s*(?:or newer|or later|and newer|\+)|(?:newer than|since|after|from)\s*(\d{4})")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
_PRICE_FIGURE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{5,})")
_NAME_SEQ = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)+)\b")
_NAME_AFTER = re.compile(r"(?:i'm|i am|this is|my name is|name is)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)?)", re.IGNORECASE)
_BROWSE = ("in stock", "inventory", "available", "on the market", "what do you have", "what have you got", "anything", "listings",
           "show me", "any ", "do you have", "looking for", "i want", "i need", "cars", "homes", "houses", "properties")
_NEED_MORE = "Could you tell me a little more, such as a make or model, or a budget? I can search the catalog, hold an item or pass your details to a person."


def _money(match: re.Match, number: str = "n", suffix: str = "s", fallback_suffix: str | None = None) -> float | None:
    raw = match.group(number)
    sfx = (match.group(suffix) if suffix in match.groupdict() else None) or fallback_suffix
    if raw == "a":
        value = 1.0
        if not sfx:
            return None
    else:
        value = float(raw.replace(",", ""))
    if sfx:
        sfx = sfx.lower()
        value *= 1_000 if sfx in ("k", "thousand") else 1_000_000
    return value


class KeywordRouter:
    def __init__(self, domain: str, catalog: list[dict]):
        self.domain = domain
        self.search_tool = cases.SEARCH_TOOLS[0] if domain == "cars" else cases.SEARCH_TOOLS[1]
        self.makes: dict[str, str] = {}
        self.models: dict[str, str] = {}
        self.cities: dict[str, str] = {}
        for row in catalog:
            try:
                attrs = json.loads(row.get("attributes") or "{}")
            except ValueError:
                attrs = {}
            if attrs.get("make"):
                self.makes[str(attrs["make"]).lower()] = str(attrs["make"])
            if attrs.get("model"):
                self.models[str(attrs["model"]).lower()] = str(attrs["model"])
            city = attrs.get("city") or str(attrs.get("location", "")).split(",")[0].strip()
            if city:
                self.cities[city.lower()] = city

    def _find(self, vocab: dict[str, str], text: str) -> str | None:
        for key in sorted(vocab, key=len, reverse=True):
            if re.search(rf"\b{re.escape(key)}s?\b", text):
                return vocab[key]
        return None

    def _price_args(self, text: str) -> dict:
        args: dict = {}
        between = _BETWEEN.search(text)
        if between:
            sfx = between.group("s")
            low = float(between.group("a").replace(",", ""))
            if between.group("as") or sfx:
                low *= 1_000 if (between.group("as") or sfx).lower() in ("k", "thousand") else 1_000_000
            high = _money(between)
            if high is not None:
                return {"price_min": int(low), "price_max": int(high)}
        for pattern, key in ((_CEILING, "price_max"), (_FLOOR, "price_min")):
            found = pattern.search(text)
            value = _money(found) if found else None
            if value is not None:
                args[key] = int(value)
        return args

    def route(self, message: str) -> tuple[str | None, dict]:
        text = message.lower()
        emails, phones = _EMAIL.findall(message), _PHONE.findall(message)
        contact = emails[0] if emails else (phones[0].strip() if phones else None)
        stripped = _PHONE.sub(" ", _EMAIL.sub(" ", message))
        make = self._find(self.makes, text) if self.domain == "cars" else None
        model = self._find(self.models, text) if self.domain == "cars" else None
        price_figure = _PRICE_FIGURE.search(stripped)

        if re.search(r"\b(?:hold|reserve|book|viewing|schedule)\b", text):
            return None, {}
        if contact and (make or model or price_figure):
            name = self._customer_name(stripped)
            if name:
                year = re.search(r"\b((?:19|20)\d{2})\b", stripped)
                title = " ".join(p for p in (year.group(1) if year else None, make, model) if p) or "Listing"
                args: dict = {"name": title, "customer_name": name, "customer_contact": contact}
                if price_figure:
                    args["price"] = int(price_figure.group(1).replace(",", ""))
                return "create_lead", args
        if contact:
            return None, {}

        args = {}
        remaining = text
        if self.domain == "cars":
            mileage = _MILEAGE.search(text)
            if mileage:
                args["mileage_max"] = int(float(mileage.group(1).replace(",", "")) * (1000 if mileage.group(2) else 1))
                remaining = text.replace(mileage.group(0), " ")
        args = {**self._price_args(remaining), **args}
        if self.domain == "cars":
            if make:
                args["make"] = make
            if model:
                args["model"] = model
            year = _YEAR_MIN.search(text)
            if year:
                args["year_min"] = int(year.group(1) or year.group(2))
            for word, value in (("certified", "Certified"), ("brand new", "New"), ("used", "Used")):
                if re.search(rf"\b{word}\b", text):
                    args["condition"] = value
                    break
            else:
                if re.search(r"\bnew\b", text):
                    args["condition"] = "New"
            for pattern, value in ((r"cheapest|lowest price", "price_asc"), (r"most expensive|highest price", "price_desc"),
                                   (r"lowest mileage|least mileage", "mileage_asc"), (r"newest", "year_desc")):
                if re.search(pattern, text):
                    args["sort_by"] = value
                    break
            location = self._location(message, text)
            if location:
                args["location"] = location
        if args or any(w in text for w in _BROWSE) and not self._off_topic(text):
            return self.search_tool, args
        return None, {}

    @staticmethod
    def _off_topic(text: str) -> bool:
        return bool(re.search(r"weather|python|javascript|ignore|instruction|prompt|poem|link|url", text))

    def _location(self, message: str, text: str) -> str | None:
        for name in sorted(STATES, key=len, reverse=True):
            if re.search(rf"\b{name}\b", text):
                return STATES[name]
        for code in re.findall(r"\bin ([A-Z]{2})\b", message):
            if code in STATES.values():
                return code
        return self._find(self.cities, text)

    def _customer_name(self, text: str) -> str | None:
        known = set(self.makes) | set(self.models) | set(self.cities)
        for sequence in _NAME_SEQ.findall(text):
            if not any(w.lower() in known for w in sequence.split()):
                return sequence
        found = _NAME_AFTER.search(text)
        return found.group(1).title() if found else None

    def reply(self, tool: str | None, result: dict | None, hint: str | None) -> str:
        if tool is None or result is None:
            return _NEED_MORE
        if tool == "create_lead":
            return "Thank you. I have passed your details to our team and a person will contact you." if "error" not in result else _NEED_MORE
        if "error" in result:
            return _NEED_MORE
        matches = result.get("matches") or []
        if not matches:
            return "Sorry, I don't have any matching items in the catalog right now."
        lines = []
        for row in matches:
            lines.append(f"{row['name']} - ${row['price']:,.0f}")
        count = result.get("count", len(matches))
        head = f"I found {count} matching items. Here are {len(matches)}:" if count > len(matches) else f"I found {count} matching item{'s' if count != 1 else ''}:"
        return head + "\n" + "\n".join(lines)


def run_router_case(case, router: KeywordRouter, adapter, *, catalog, reference, repeat, domain, set_name) -> dict:
    expected = cases.build_expected(case, catalog, reference)
    start = time.monotonic()
    tool, args = router.route(case["message"])
    calls, results = [], []
    result = None
    if tool:
        try:
            parsed = parse_tool_args(adapter.tool_models()[tool], args)
            result = adapter.execute_tool(tool, parsed)
        except Exception as exc:  # noqa: BLE001 - a baseline records what its tool call did
            result = {"error": str(exc)}
        calls.append({"name": tool, "arguments": args})
        results.append(result)
    return {
        "case_id": case["id"], "category": case.get("category"), "set": set_name, "domain": domain, "repeat": repeat,
        "message": case["message"], "history": case.get("history"), "expected": expected,
        "latency_ms": (time.monotonic() - start) * 1000, "models": [ROUTER_MODEL], "tokens": {"prompt": 0, "completion": 0}, "cost": 0.0,
        "actual": {"reply": router.reply(tool, result, None), "tool_calls_made": calls, "tool_results": results},
    }


def no_tools_prompt(domain: str, catalog: list[dict]) -> str:
    """The whole baseline prompt: role, limits, then one compact line per available item."""
    kind = "car dealership" if domain == "cars" else "real estate agency"
    lines = []
    for row in catalog:
        try:
            a = json.loads(row.get("attributes") or "{}")
        except ValueError:
            a = {}
        if domain == "cars":
            detail = f"{a.get('mileage', '?')} mi | {a.get('condition', '?')} | {a.get('location', '?')}"
        else:
            detail = f"{a.get('bed', '?')}bd/{a.get('bath', '?')}ba | {a.get('city', '?')}, {a.get('state', '?')} | {a.get('house_size', '?')} sqft"
        lines.append(f"{row['id']} | {row['name']} | ${row['price']:,.0f} | {detail}")
    return (
        f"You are a sales assistant for a {kind}. You have no tools: you cannot search, hold, book or save anything. "
        "Answer the customer using only the catalog below. If something is not in the catalog, say so. Never invent items "
        "or prices, and never send links. If the customer wants to buy, ask for their name and an email or phone number "
        "and say a person will follow up.\n\nCatalog:\n" + "\n".join(lines)
    )


def run_no_tools_case(case, adapter, llm, *, catalog, reference, repeat, domain, set_name) -> dict:
    expected = cases.build_expected(case, catalog, reference)
    messages = [{"role": "system", "content": no_tools_prompt(domain, catalog)}]
    messages += [{"role": m["role"], "content": m["content"]} for m in case.get("history") or []]
    messages.append({"role": "user", "content": case["message"]})
    record = {
        "case_id": case["id"], "category": case.get("category"), "set": set_name, "domain": domain, "repeat": repeat,
        "message": case["message"], "history": case.get("history"), "expected": expected,
        "models": [], "tokens": {"prompt": 0, "completion": 0}, "cost": 0.0,
    }
    start = time.monotonic()
    try:
        response = llm.chat(messages, [])
    except Exception as exc:  # noqa: BLE001 - recorded as this case's error, not raised
        from eval import budget

        if isinstance(exc, (budget.BudgetExhausted, budget.DailyAllowanceExhausted)):
            raise
        record["latency_ms"] = (time.monotonic() - start) * 1000
        record["actual"] = {"error": f"{type(exc).__name__}: {exc}", "reply": None, "tool_calls_made": [], "tool_results": []}
        return record
    record["latency_ms"] = (time.monotonic() - start) * 1000
    record["models"] = [response.model or "unknown"]
    record["tokens"] = {"prompt": response.usage.prompt_tokens, "completion": response.usage.completion_tokens}
    record["cost"] = response.usage.cost or 0.0
    record["actual"] = {"reply": response.content or "", "tool_calls_made": [], "tool_results": [], "evidence_rows": catalog}
    return record
