"""Live-call budget, pacing and rate-limit reading for the eval.

`BudgetedLLM` wraps a chat model. Every provider attempt (a failover attempt counts) is added to a ledger
file before the next call, and the wrapper raises `BudgetExhausted` once the cap is reached, so a restart
cannot overspend. Calls are spaced `min_interval` seconds apart. `parse_rate_limit` reads a 429 so the
runner can wait the right time or stop cleanly when the day's allowance is gone.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WAIT_SECONDS = 30.0
RATE_LIMIT_RETRIES = 3
# Payment required, key rejected, forbidden: waiting will not help, so the run stops instead of recording errors.
QUOTA_STATUSES = (401, 402, 403)
MAX_WAIT_SECONDS = 180.0
# A reset further away than this is a daily allowance, not a per-minute window.
DAILY_RESET_SECONDS = 3600.0


class BudgetExhausted(Exception):
    """The cap on live LLM calls for this phase is used up."""


class DailyAllowanceExhausted(Exception):
    """The provider says today's free allowance is gone."""


def read_ledger(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"calls": 0, "by_label": {}}


@dataclass
class RateLimitInfo:
    wait_seconds: float
    daily_exhausted: bool
    limit: int | None = None
    remaining: int | None = None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_rate_limit(response, now: float | None = None) -> RateLimitInfo:
    """What a 429 says: how long to wait, whether the daily allowance is spent, and the limit numbers."""
    now = time.time() if now is None else now
    headers = {k.lower(): v for k, v in response.headers.items()}
    message = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message", ""))
            meta = (error.get("metadata") or {}).get("headers") or {}
            headers.update({str(k).lower(): v for k, v in meta.items()})
    except ValueError:
        pass
    limit, remaining = _int(headers.get("x-ratelimit-limit")), _int(headers.get("x-ratelimit-remaining"))
    wait = _int(headers.get("retry-after"))
    daily = any(w in message.lower() for w in ("per-day", "per day", "daily"))
    reset = _int(headers.get("x-ratelimit-reset"))
    if reset is not None:
        seconds = reset / 1000 - now
        if seconds > DAILY_RESET_SECONDS:
            daily = True
        wait = wait if wait is not None else max(seconds, 1.0)
    return RateLimitInfo(float(wait) if wait is not None else DEFAULT_WAIT_SECONDS, daily, limit, remaining)


class BudgetedLLM:
    def __init__(
        self, inner, ledger: Path, *, cap: int, label: str, min_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    ):
        self.inner = inner
        self.ledger = Path(ledger)
        self.cap = cap
        self.label = label
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.daily_exhausted = False

    def _add(self, n: int) -> None:
        data = read_ledger(self.ledger)
        data["calls"] = data.get("calls", 0) + n
        data["cap"] = self.cap
        by = data.setdefault("by_label", {})
        by[self.label] = by.get(self.label, 0) + n
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def chat(self, messages, tools):
        return self._ask(messages, tools, "chat")

    def chat_requiring_a_tool(self, messages, tools):
        return self._ask(messages, tools, "chat_requiring_a_tool")

    def _ask(self, messages, tools, method):
        if read_ledger(self.ledger).get("calls", 0) >= self.cap:
            raise BudgetExhausted(f"{self.cap} live LLM calls used")
        if self._last is not None:
            gap = self.min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            if read_ledger(self.ledger).get("calls", 0) >= self.cap:
                raise BudgetExhausted(f"{self.cap} live LLM calls used")
            try:
                response = getattr(self.inner, method, self.inner.chat)(messages, tools)
            except Exception as exc:
                self._add(max(1, len([a for a in getattr(exc, "attempts", []) if a.outcome != "cooldown"])))
                response_obj = getattr(exc, "response", None)
                if response_obj is not None and getattr(response_obj, "status_code", None) in QUOTA_STATUSES:
                    self.daily_exhausted = True
                    raise DailyAllowanceExhausted(f"HTTP {response_obj.status_code}: the account has no quota or billing") from exc
                if response_obj is None or getattr(response_obj, "status_code", None) != 429:
                    raise
                info = parse_rate_limit(response_obj)
                if info.daily_exhausted:
                    self.daily_exhausted = True
                    raise
                if attempt >= RATE_LIMIT_RETRIES:
                    raise
                self._sleep(min(max(info.wait_seconds, 1.0), MAX_WAIT_SECONDS))
                continue
            self._add(max(1, len([a for a in response.attempts if a.outcome != "cooldown"])))
            return response
        raise AssertionError("unreachable")
