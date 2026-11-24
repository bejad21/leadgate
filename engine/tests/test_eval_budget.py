import json

import httpx
import pytest

from engine.llm_client import Attempt, LLMResponse
from eval import budget


def response(attempts=1):
    return LLMResponse(content="hi", tool_calls=[], attempts=[Attempt("m", "ok")] * attempts)


class Inner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def chat(self, messages, tools):
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(tmp_path, inner, cap=3, sleeps=None):
    return budget.BudgetedLLM(inner, tmp_path / "ledger.json", cap=cap, label="t", min_interval=0, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_every_call_is_counted_and_saved(tmp_path):
    llm = make(tmp_path, Inner([response(), response()]))
    llm.chat([], [])
    llm.chat([], [])
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["calls"] == 2 and ledger["by_label"] == {"t": 2}


def test_failover_attempts_each_count_as_a_call(tmp_path):
    llm = make(tmp_path, Inner([response(attempts=2)]))
    llm.chat([], [])
    assert budget.read_ledger(tmp_path / "ledger.json")["calls"] == 2


def test_a_failed_call_still_counts(tmp_path):
    llm = make(tmp_path, Inner([RuntimeError("x")]))
    with pytest.raises(RuntimeError):
        llm.chat([], [])
    assert budget.read_ledger(tmp_path / "ledger.json")["calls"] == 1


def test_the_cap_stops_the_run_before_the_call_is_made(tmp_path):
    inner = Inner([response(), response(), response(), response()])
    llm = make(tmp_path, inner, cap=2)
    llm.chat([], [])
    llm.chat([], [])
    with pytest.raises(budget.BudgetExhausted):
        llm.chat([], [])
    assert len(inner.outcomes) == 2


def test_the_cap_survives_a_restart(tmp_path):
    make(tmp_path, Inner([response(), response()]), cap=2).chat([], [])
    again = make(tmp_path, Inner([response(), response()]), cap=2)
    again.chat([], [])
    with pytest.raises(budget.BudgetExhausted):
        again.chat([], [])


def test_calls_are_paced(tmp_path):
    sleeps = []
    clock = iter([0.0, 0.5, 0.5, 5.0, 5.0, 5.0])
    llm = budget.BudgetedLLM(Inner([response(), response()]), tmp_path / "l.json", cap=9, label="t", min_interval=3,
                             sleep=sleeps.append, clock=lambda: next(clock))
    llm.chat([], [])
    llm.chat([], [])
    assert sleeps and sleeps[0] == pytest.approx(2.5)


def status_error(code, headers=None, body=None):
    request = httpx.Request("POST", "https://x/y")
    return httpx.HTTPStatusError("e", request=request, response=httpx.Response(code, headers=headers or {}, json=body or {}, request=request))


def test_a_per_minute_limit_says_how_long_to_wait():
    exc = status_error(429, {"retry-after": "12"}, {"error": {"message": "Rate limit exceeded: limit_rpm"}})
    info = budget.parse_rate_limit(exc.response, now=1000.0)
    assert info.wait_seconds == 12 and not info.daily_exhausted


def test_a_daily_limit_is_recognised_from_the_message():
    body = {"error": {"message": "Rate limit exceeded: free-models-per-day. Add credits"}}
    assert budget.parse_rate_limit(status_error(429, {}, body).response, now=0).daily_exhausted


def test_a_reset_far_in_the_future_means_the_day_is_used_up():
    body = {"error": {"message": "limit", "metadata": {"headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int((1000 + 6 * 3600) * 1000))}}}}
    info = budget.parse_rate_limit(status_error(429, {}, body).response, now=1000.0)
    assert info.daily_exhausted and info.remaining == 0


def test_reset_headers_are_read_as_milliseconds_since_the_epoch():
    headers = {"x-ratelimit-reset": str(int(1030 * 1000)), "x-ratelimit-limit": "20", "x-ratelimit-remaining": "0"}
    info = budget.parse_rate_limit(status_error(429, headers).response, now=1000.0)
    assert info.wait_seconds == pytest.approx(30) and info.limit == 20 and not info.daily_exhausted


def test_a_response_with_no_hints_falls_back_to_a_default_wait():
    assert budget.parse_rate_limit(status_error(429).response, now=0).wait_seconds == budget.DEFAULT_WAIT_SECONDS


def test_a_per_minute_429_is_waited_out_and_retried_inside_the_wrapper(tmp_path):
    sleeps = []
    inner = Inner([status_error(429, {"retry-after": "5"}), response()])
    llm = make(tmp_path, inner, cap=9, sleeps=sleeps)
    assert llm.chat([], []).content == "hi"
    assert sleeps == [5] and budget.read_ledger(tmp_path / "ledger.json")["calls"] == 2


def test_a_daily_429_sets_a_flag_and_is_raised(tmp_path):
    body = {"error": {"message": "free-models-per-day"}}
    llm = make(tmp_path, Inner([status_error(429, {}, body)]), cap=9)
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat([], [])
    assert llm.daily_exhausted is True


@pytest.mark.parametrize("code", [401, 402, 403])
def test_a_payment_or_key_error_stops_the_run_instead_of_being_recorded(tmp_path, code):
    llm = make(tmp_path, Inner([status_error(code)]), cap=9)
    with pytest.raises(budget.DailyAllowanceExhausted, match=str(code)):
        llm.chat([], [])
    assert llm.daily_exhausted is True
