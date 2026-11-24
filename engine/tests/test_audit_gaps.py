"""Tests for paths that were only exercised live: a hold that the model calls "confirmed", the forced-tool call through
the failover and budget wrappers, and the rescore script."""
import json

import httpx

from engine.core.agent_loop import run_turn
from engine.llm_client import FailoverLLM, LLMResponse, ToolCall
from engine.tests.test_agent_habits import SEARCH_RESULT, ShopAdapter, calls, says
from engine.tests.test_agent_loop import FakeLLMClient


def test_a_reply_that_calls_a_real_hold_confirmed_is_replaced_by_the_plain_wording():
    reserve = ("reserve_item", {"item_id": 7, "customer_name": "Dana Reyes", "customer_contact": "dana@example.com"})
    history = [
        {"role": "user", "content": "any Toyota?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "e1", "type": "function", "function": {"name": "search_inventory", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "e1", "name": "search_inventory", "content": json.dumps(SEARCH_RESULT)},
        {"role": "assistant", "content": "One: the 2020 Toyota Camry."},
        {"role": "user", "content": "Hold it please. Dana Reyes, dana@example.com"},
    ]
    llm = FakeLLMClient([calls(reserve), says("Great news Dana, your hold is confirmed and the car is yours!")])
    result = run_turn(history, ShopAdapter(), llm)
    assert "not a confirmation" in result.reply and "confirmed" not in result.reply.lower().replace("not a confirmation", "")
    assert "hold request" in result.reply


class Plain:
    """A model that has no way to be told to call a tool."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append("chat")
        return LLMResponse(content="ok", tool_calls=[])


class Forcing(Plain):
    def chat_requiring_a_tool(self, messages, tools):
        self.calls.append("forced")
        return LLMResponse(content=None, tool_calls=[ToolCall(name="t", arguments={})])


def test_failover_passes_the_requirement_on_and_falls_back_for_a_client_that_cannot_honour_it():
    forcing, plain = Forcing(), Plain()
    assert FailoverLLM([forcing]).chat_requiring_a_tool([], []).tool_calls
    assert forcing.calls == ["forced"]
    assert FailoverLLM([plain]).chat_requiring_a_tool([], []).content == "ok"
    assert plain.calls == ["chat"]


def test_failover_moves_on_when_the_forced_call_fails_on_the_first_model():
    class Down(Forcing):
        def chat_requiring_a_tool(self, messages, tools):
            raise httpx.ConnectError("down")

    second = Forcing()
    response = FailoverLLM([Down(), second]).chat_requiring_a_tool([], [])
    assert response.tool_calls and second.calls == ["forced"]


def test_the_budget_wrapper_counts_and_forwards_a_forced_call(tmp_path):
    from eval.budget import BudgetedLLM, read_ledger

    inner = Forcing()
    budgeted = BudgetedLLM(inner, tmp_path / "ledger.json", cap=10, label="t", min_interval=0)
    assert budgeted.chat_requiring_a_tool([], []).tool_calls
    assert inner.calls == ["forced"] and read_ledger(tmp_path / "ledger.json")["calls"] == 1


def test_rescore_lists_only_the_runs_whose_result_changed(tmp_path, capsys):
    from engine.tests.test_eval_run import header
    from eval import rescore, results_io

    folder = tmp_path / "2026-01-01-x"
    folder.mkdir()
    path = folder / "cars_dev.jsonl"
    results_io.start_run(path, header(repeats=1))
    expected = {"tool": None, "accept_tools": [], "args": {}, "rules": [{"rule": "reply_nonempty"}], "resolution_errors": []}
    for i, reply in enumerate(["hello", ""]):
        results_io.append_record(path, {"case_id": f"c{i}", "repeat": 0, "expected": expected, "actual": {"reply": reply, "tool_calls_made": [], "tool_results": []}})
    now = rescore.grade_all_runs(tmp_path)
    assert now["2026-01-01-x/cars_dev.jsonl"] == {"passed": 1, "scored": 2}
    before = tmp_path / "before.json"
    before.write_text(json.dumps({"2026-01-01-x/cars_dev.jsonl": {"passed": 0, "scored": 2}}), encoding="utf-8")
    assert rescore.main(["--results-root", str(tmp_path), "--compare", str(before)]) == 0
    out = capsys.readouterr().out
    assert "cars_dev.jsonl | 2 | 0 | 1 | 0.0% | 50.0%" in out
    same = tmp_path / "same.json"
    same.write_text(json.dumps(now), encoding="utf-8")
    rescore.main(["--results-root", str(tmp_path), "--compare", str(same)])
    assert "cars_dev" not in capsys.readouterr().out.split("| Run |")[-1].split("|---|---|---|---|---|---|")[-1]


def test_a_provider_that_refuses_tool_choice_is_asked_again_without_it():
    from engine.core.agent_loop import _chat_requiring_a_tool

    class Refuses(Plain):
        def chat_requiring_a_tool(self, messages, tools):
            request = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, request=request))

    model = Refuses()
    assert _chat_requiring_a_tool(model, [], []).content == "ok" and model.calls == ["chat"]


def test_other_errors_from_a_forced_call_are_not_hidden():
    import pytest

    from engine.core.agent_loop import _chat_requiring_a_tool

    class Down(Plain):
        def chat_requiring_a_tool(self, messages, tools):
            request = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError("down", request=request, response=httpx.Response(503, request=request))

    with pytest.raises(httpx.HTTPStatusError):
        _chat_requiring_a_tool(Down(), [], [])
