import datetime as dt
import json

import httpx
import pytest

from engine.core.agent_loop import SYSTEM_PROMPT
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.test_agent_loop import FakeCarsAdapter, FakeLLMClient
from eval import budget, results_io, run_eval

REF = dt.date(2026, 11, 4)
CASES = [
    {"id": "a", "category": "x", "message": "toyotas", "expected_tool": "search_inventory", "expected_args": {"make": "Toyota"}, "expected_outcome": "o"},
    {"id": "b", "category": "y", "message": "weather?", "expected_tool": None, "expected_args": {}, "expected_outcome": "o"},
]


def header(repeats=2):
    return results_io.build_header(
        kind="agent", domain="cars", dataset_name="d.json", dataset_sha256="0" * 64, repeats=repeats, reference_date="2026-11-04",
        sampling={"temperature": 0.0, "max_tokens": 800, "seed": None}, prompt_version="p", models_configured=["m"],
        argv=["x"], run_date="2026-11-04", git_sha="s", git_dirty=False,
    )


def fake_record(case, adapter, llm, *, catalog, reference, repeat, domain, set_name):
    return {"case_id": case["id"], "repeat": repeat, "actual": {"reply": "ok"}}


def run(tmp_path, repeats=2, runner=fake_record, items=CASES):
    return run_eval.run_dataset(items, None, None, catalog=[], reference=REF, path=tmp_path / "r.jsonl", header=header(repeats),
                                domain="cars", set_name="dev", repeats=repeats, runner=runner, log=lambda *_: None)


def test_every_case_is_run_once_per_repeat_and_the_header_comes_first(tmp_path):
    outcome = run(tmp_path)
    assert outcome["complete"] and outcome["recorded"] == 4
    lines = (tmp_path / "r.jsonl").read_text().splitlines()
    assert "_header" in json.loads(lines[0])
    assert [(r["case_id"], r["repeat"]) for r in results_io.read_run(tmp_path / "r.jsonl").records] == [("a", 0), ("b", 0), ("a", 1), ("b", 1)]


def test_a_restart_runs_only_what_is_missing(tmp_path):
    calls = []

    def stop_after_two(case, *a, **kw):
        calls.append((case["id"], kw["repeat"]))
        if len(calls) == 3:
            raise budget.BudgetExhausted("cap")
        return fake_record(case, *a, **kw)

    first = run(tmp_path, runner=stop_after_two)
    assert not first["complete"] and "BudgetExhausted" in first["reason"]
    calls.clear()

    def counting(case, *a, **kw):
        calls.append((case["id"], kw["repeat"]))
        return fake_record(case, *a, **kw)

    second = run(tmp_path, runner=counting)
    assert second["complete"] and calls == [("a", 1), ("b", 1)]


def test_an_errored_record_is_retried_on_restart_and_a_run_with_errors_is_not_complete(tmp_path):
    def failing(case, *a, **kw):
        record = fake_record(case, *a, **kw)
        if case["id"] == "b":
            record["actual"] = {"error": "HTTPStatusError"}
        return record

    first = run(tmp_path, repeats=1, runner=failing)
    assert not first["complete"] and first["failed"] == 1
    second = run(tmp_path, repeats=1)
    assert second["complete"] and second["recorded"] == 1


def test_a_daily_allowance_stops_the_run_cleanly_and_says_so(tmp_path):
    def spent(case, *a, **kw):
        raise budget.DailyAllowanceExhausted("gone")

    outcome = run(tmp_path, runner=spent)
    assert not outcome["complete"] and "DailyAllowanceExhausted" in outcome["reason"]
    footer = results_io.read_run(tmp_path / "r.jsonl").footer
    assert footer is not None and footer["complete"] is False


def test_resuming_with_a_different_dataset_is_refused(tmp_path):
    run(tmp_path)
    other = header()
    other["dataset"] = {"name": "d.json", "sha256": "1" * 64}
    with pytest.raises(results_io.ProvenanceError):
        run_eval.run_dataset(CASES, None, None, catalog=[], reference=REF, path=tmp_path / "r.jsonl", header=other,
                             domain="cars", set_name="dev", repeats=2, runner=fake_record, log=lambda *_: None)


def answer(text=None, calls=()):
    return LLMResponse(content=text, tool_calls=list(calls))


def test_run_case_records_the_expectation_the_tools_and_the_models():
    adapter = FakeCarsAdapter({"matches": [{"id": 1, "name": "2020 Toyota Camry", "price": 100.0, "attributes": "{}"}], "count": 1})
    llm = FakeLLMClient([answer(calls=[ToolCall(name="search_inventory", arguments={"make": "Toyota"}, id="c1")]), answer("Found one for $100.")])
    record = run_eval.run_case(CASES[0], adapter, llm, catalog=[], reference=REF, repeat=3, domain="cars", set_name="dev")
    assert record["repeat"] == 3 and record["expected"]["tool"] == "search_inventory"
    assert record["actual"]["tool_calls_made"][0]["name"] == "search_inventory"
    assert record["actual"]["reply"] == "Found one for $100."
    assert len(record["models"]) == 2


def test_run_case_gives_the_model_the_rebuilt_history():
    catalog = [{"id": 7, "name": "2020 Toyota Camry", "price": 21834.0, "attributes": "{}"}]
    seen = {}

    class Spy(FakeLLMClient):
        def chat(self, messages, tools):
            seen["roles"] = [m["role"] for m in messages]
            return super().chat(messages, tools)

    case = {**CASES[1], "history": [{"role": "user", "content": "camry?"}, {"role": "assistant", "content": "One at $21,834."}]}
    run_eval.run_case(case, FakeCarsAdapter({}), Spy([answer("hello there")]), catalog=catalog, reference=REF, repeat=0, domain="cars", set_name="dev")
    assert seen["roles"] == ["system", "user", "assistant", "tool", "assistant", "user"]


def http_error(code, body=None, headers=None):
    request = httpx.Request("POST", "https://x")
    return httpx.HTTPStatusError("e", request=request, response=httpx.Response(code, json=body or {}, headers=headers or {}, request=request))


class Flaky:
    def __init__(self, errors, then):
        self.errors, self.then = list(errors), then

    def chat(self, messages, tools):
        if self.errors:
            raise self.errors.pop(0)
        return self.then


def test_a_rate_limit_is_waited_out_for_as_long_as_the_provider_says():
    sleeps = []
    llm = Flaky([http_error(429, headers={"retry-after": "7"})], answer("hello there"))
    result, error = run_eval.run_turn_with_retry([{"role": "user", "content": "hi"}], FakeCarsAdapter({}), llm, sleep=sleeps.append)
    assert error is None and result is not None and sleeps == [7]


def test_a_spent_daily_allowance_is_raised_not_retried():
    llm = Flaky([http_error(429, {"error": {"message": "free-models-per-day"}})], answer("x"))
    with pytest.raises(budget.DailyAllowanceExhausted):
        run_eval.run_turn_with_retry([{"role": "user", "content": "hi"}], FakeCarsAdapter({}), llm, sleep=lambda s: None)


def test_persistent_429s_end_as_a_recorded_error():
    llm = Flaky([http_error(429)] * 10, answer("x"))
    result, error = run_eval.run_turn_with_retry([{"role": "user", "content": "hi"}], FakeCarsAdapter({}), llm, sleep=lambda s: None, max_retries=2)
    assert result is None and isinstance(error, httpx.HTTPStatusError)


def test_a_retry_starts_from_the_original_history_not_from_the_failed_attempt():
    seen = []

    class TimesOutAfterTheTool:
        script = [
            answer(calls=[ToolCall(name="search_inventory", arguments={"make": "Toyota"}, id="c1")]),
            httpx.ReadTimeout("slow"),
            answer(calls=[ToolCall(name="search_inventory", arguments={"make": "Toyota"}, id="c2")]),
            answer("Found one for $100."),
        ]

        def chat(self, messages, tools):
            seen.append([m["role"] for m in messages])
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    adapter = FakeCarsAdapter({"matches": [{"id": 1, "name": "2020 Toyota Camry", "price": 100.0, "attributes": "{}"}], "count": 1})
    history = [{"role": "user", "content": "any Toyota?"}]
    result, error = run_eval.run_turn_with_retry(history, adapter, TimesOutAfterTheTool(), sleep=lambda s: None)
    assert error is None and result is not None
    assert seen[0] == seen[2]  # the retry's first call saw the same conversation as the first attempt's first call
    assert [c.name for c in result.tool_calls_made] == ["search_inventory"]


def test_the_model_is_told_the_reference_date(monkeypatch):
    import engine.core.agent_loop as loop

    monkeypatch.setattr(loop, "system_prompt", loop.system_prompt)
    run_eval.freeze_today(REF)
    assert loop.system_prompt().endswith("Today's date is 2026-11-04.") and loop.system_prompt().startswith(SYSTEM_PROMPT)


def test_a_limited_run_may_not_write_under_the_committed_results():
    with pytest.raises(SystemExit):
        run_eval.main(["--domain", "cars", "--limit", "1", "--results-dir", str(run_eval.RESULTS_DIR / "x")])


def test_the_guardrails_off_flag_names_what_it_switches_off():
    from eval.guardrails_off import GUARDRAILS

    assert run_eval.guardrail_names(None) == set()
    assert run_eval.guardrail_names("all") == set(GUARDRAILS)
    assert run_eval.guardrail_names("tool_call_cap, reply_filter") == {"tool_call_cap", "reply_filter"}


def test_main_runs_end_to_end_with_fakes_and_writes_a_complete_headed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "load_config", lambda: {})
    llm = FakeLLMClient([answer("Which make and budget do you have in mind?")] * 3)
    llm.model = "fake/model"  # type: ignore[attr-defined]
    monkeypatch.setattr(run_eval, "get_llm_client", lambda config: llm)
    monkeypatch.setattr(run_eval, "build_adapter", lambda domain, config: FakeCarsAdapter({}))
    monkeypatch.setattr(run_eval, "load_catalog", lambda adapter, domain: [])
    monkeypatch.setattr(run_eval, "LEDGER_PATH", tmp_path / "ledger.json")
    code = run_eval.main(["--domain", "cars", "--set", "dev", "--limit", "2", "--repeats", "1", "--pace", "0", "--results-dir", str(tmp_path / "out")])
    assert code == 0
    run = results_io.read_run(tmp_path / "out" / "cars_dev.jsonl")
    assert run.header["model_configured"] == ["fake/model"] and run.header["prompt_version"]
    assert run.header["dataset"]["name"] == "cars_test_set.json" and len(run.records) == 2
    assert run.footer and run.footer["complete"] is True
    assert json.loads((tmp_path / "ledger.json").read_text())["calls"] == 2


def test_a_quota_error_from_the_model_ends_the_turn_retry_loop_for_good():
    class Blocked:
        def chat(self, messages, tools):
            raise budget.DailyAllowanceExhausted("HTTP 402")

    with pytest.raises(budget.DailyAllowanceExhausted):
        run_eval.run_turn_with_retry([{"role": "user", "content": "hi"}], FakeCarsAdapter({}), Blocked(), sleep=lambda s: None)
