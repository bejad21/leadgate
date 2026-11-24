"""The red-team negative control switches the guardrails off by patching the agent loop. It must keep
working when the loop changes: a baseline that raises on the first tool call is no baseline."""
import pytest

import engine.adapters.cars as cars_module
import engine.core.agent_loop as loop
import engine.main as main_module
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.test_agent_loop import FakeCarsAdapter, FakeLLMClient

PATCHED_LOOP_NAMES = [
    "_run_guarded_tool",
    "_with_customer_contact",
    "sanitize_tool_result",
    "WRITES_PER_TURN",
    "parse_tool_args",
    "filter_reply",
    "TOOL_CALL_CAP",
    "SYSTEM_PROMPT",
]


@pytest.fixture
def guardrails_off(monkeypatch):
    """Switch the guardrails off, and put every patched name back when the test ends."""
    from eval.guardrails_off import disable_guardrails

    for name in PATCHED_LOOP_NAMES:
        monkeypatch.setattr(loop, name, getattr(loop, name))
    monkeypatch.setattr(cars_module, "create_verified_lead", cars_module.create_verified_lead)
    monkeypatch.setattr(main_module, "is_injection_attempt", main_module.is_injection_attempt)
    monkeypatch.setattr(main_module, "_write_limiter", main_module._write_limiter)
    restore = disable_guardrails(main_module)
    yield
    restore()


def test_a_tool_call_runs_and_the_models_reply_comes_back_with_the_guardrails_off(guardrails_off):
    adapter = FakeCarsAdapter({"matches": [{"name": "Corolla"}], "count": 1})
    call = ToolCall(name="search_inventory", arguments={"make": "Toyota"}, id="c1")
    llm = FakeLLMClient([LLMResponse(content=None, tool_calls=[call]), LLMResponse(content="Here is a Corolla.", tool_calls=[])])

    result = loop.run_turn([{"role": "user", "content": "a Toyota"}], adapter, llm)

    assert adapter.executed_calls == [("search_inventory", {"make": "Toyota"})]
    assert result.reply == "Here is a Corolla."
    assert result.trace is not None and [t.outcome for t in result.trace.tools] == ["ok"]


def test_a_tool_that_raises_is_reported_to_the_model_with_the_guardrails_off(guardrails_off):
    class Failing(FakeCarsAdapter):
        def execute_tool(self, name, args):
            raise RuntimeError("odoo is down")

    call = ToolCall(name="search_inventory", arguments={}, id="c1")
    llm = FakeLLMClient([LLMResponse(content=None, tool_calls=[call]), LLMResponse(content="Sorry.", tool_calls=[])])

    result = loop.run_turn([{"role": "user", "content": "a Toyota"}], Failing({}), llm)

    assert result.tool_results == [{"error": "odoo is down"}]
    assert result.reply == "Sorry."
