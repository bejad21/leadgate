import json

from engine.core.adapter_base import DomainAdapter
from engine.core.agent_loop import AgentTurnResult, run_turn
from engine.llm_client import LLMResponse, ToolCall


class FakeLLMClient:
    """Test double for LLMClient: returns canned responses in sequence and
    records every call's message history so the test can assert on it."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self._responses.pop(0)


class FakeCarsAdapter(DomainAdapter):
    """Test double for a DomainAdapter: returns a canned tool schema and a
    canned execute_tool result, recording the call for assertions."""

    def __init__(self, tool_result: dict):
        self._tool_result = tool_result
        self.executed_calls: list[tuple[str, dict]] = []

    def tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_inventory",
                    "parameters": {
                        "type": "object",
                        "properties": {"make": {"type": "string"}, "price_max": {"type": "number"}},
                        "required": [],
                    },
                },
            }
        ]

    def execute_tool(self, name: str, args: dict) -> dict:
        self.executed_calls.append((name, args))
        return self._tool_result


def test_run_turn_executes_tool_and_grounds_reply():
    tool_result = {"matches": [{"name": "Toyota Corolla", "price": 18000}], "count": 1}
    tool_call = ToolCall(name="search_inventory", arguments={"make": "Toyota", "price_max": 30000})

    first_response = LLMResponse(content=None, tool_calls=[tool_call])
    second_response = LLMResponse(
        content="We have a Toyota Corolla for $18,000 available.", tool_calls=[]
    )

    llm = FakeLLMClient([first_response, second_response])
    adapter = FakeCarsAdapter(tool_result)

    history = [{"role": "user", "content": "do you have any Toyota under 30000"}]

    result = run_turn(history, adapter, llm)

    # The adapter's tool was actually invoked with the LLM's requested args.
    assert adapter.executed_calls == [("search_inventory", {"make": "Toyota", "price_max": 30000})]

    # Two LLM calls were made: the initial tool-selection call, and the
    # follow-up call that turns the tool result into a natural-language reply.
    assert len(llm.calls) == 2

    # Grounding mechanism: the second call's message history must include
    # the actual tool result, not just the original user message - this is
    # what prevents the reply from being a hallucination.
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "search_inventory"
    assert json.loads(tool_messages[0]["content"]) == tool_result

    # Protocol compliance: the assistant message carrying the original
    # tool_calls must be appended BEFORE the tool result message, and the
    # tool message's tool_call_id must match the assistant's tool_calls id.
    assistant_messages = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assistant_tool_calls = assistant_messages[0]["tool_calls"]
    assert len(assistant_tool_calls) == 1
    assert assistant_tool_calls[0]["type"] == "function"
    assert assistant_tool_calls[0]["function"]["name"] == "search_inventory"
    assert json.loads(assistant_tool_calls[0]["function"]["arguments"]) == {
        "make": "Toyota",
        "price_max": 30000,
    }
    assert tool_messages[0]["tool_call_id"] == assistant_tool_calls[0]["id"]

    assistant_index = second_call_messages.index(assistant_messages[0])
    tool_index = second_call_messages.index(tool_messages[0])
    assert assistant_index < tool_index

    # The second follow-up call must not offer tools (it's asked to
    # summarize, not to call more tools).
    assert llm.calls[1]["tools"] == []

    assert isinstance(result, AgentTurnResult)
    assert result.reply == "We have a Toyota Corolla for $18,000 available."
    assert result.tool_calls_made == [tool_call]
    assert result.tool_results == [tool_result]


def test_run_turn_uses_real_tool_call_id_when_provided():
    """When the LLM response includes a real tool_call id (as OpenRouter's
    API actually does), that id -- not a generated fallback -- must be the
    one used to link the assistant message to the tool result message."""
    tool_result = {"matches": [], "count": 0}
    tool_call = ToolCall(name="search_inventory", arguments={}, id="call_abc123")

    first_response = LLMResponse(content=None, tool_calls=[tool_call])
    second_response = LLMResponse(content="No matches found.", tool_calls=[])

    llm = FakeLLMClient([first_response, second_response])
    adapter = FakeCarsAdapter(tool_result)
    history = [{"role": "user", "content": "anything under 5000?"}]

    run_turn(history, adapter, llm)

    tool_message = next(m for m in history if m.get("role") == "tool")
    assistant_message = next(m for m in history if m.get("role") == "assistant")

    assert tool_message["tool_call_id"] == "call_abc123"
    assert assistant_message["tool_calls"][0]["id"] == "call_abc123"


# ---- v2 guardrails in the loop --------------------------------------------

import pytest

from engine.core import guardrails
from engine.core.agent_loop import SYSTEM_PROMPT
from engine.rate_limiter import FixedWindowRateLimiter


class LeadAdapter(FakeCarsAdapter):
    def tool_schemas(self):
        return super().tool_schemas() + [
            {
                "type": "function",
                "function": {
                    "name": "create_lead",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "customer_name": {"type": "string"}},
                        "required": ["name", "customer_name"],
                    },
                },
            }
        ]


def _turn(calls, final="Done."):
    return FakeLLMClient([LLMResponse(content=None, tool_calls=calls), LLMResponse(content=final, tool_calls=[])])


def _tool_messages(history):
    return [json.loads(m["content"]) for m in history if m["role"] == "tool"]


def test_unknown_tool_is_not_executed():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi"}]
    run_turn(history, adapter, _turn([ToolCall(name="delete_everything", arguments={})]))
    assert adapter.executed_calls == []
    assert "error" in _tool_messages(history)[0]


def test_unknown_arguments_are_dropped_before_execution():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi"}]
    run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={"make": "Toyota", "admin": True})]))
    assert adapter.executed_calls == [("search_inventory", {"make": "Toyota"})]


def test_invalid_argument_becomes_error_result_without_executing():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi"}]
    run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={"price_max": -5})]))
    assert adapter.executed_calls == []
    assert "error" in _tool_messages(history)[0]


def test_tool_calls_per_turn_are_capped_but_every_call_gets_a_tool_message():
    adapter = FakeCarsAdapter({"ok": True})
    calls = [ToolCall(name="search_inventory", arguments={"make": f"m{i}"}, id=f"c{i}") for i in range(6)]
    history = [{"role": "user", "content": "hi"}]
    run_turn(history, adapter, _turn(calls))
    assert len(adapter.executed_calls) == guardrails.TOOL_CALL_CAP
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == [f"c{i}" for i in range(6)]


def test_write_tool_is_rate_limited_per_chat():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)
    lead = lambda: ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})
    run_turn([{"role": "user", "content": "a"}], adapter, _turn([lead()]), chat_id=7, write_limiter=limiter)
    history = [{"role": "user", "content": "b"}]
    run_turn(history, adapter, _turn([lead()]), chat_id=7, write_limiter=limiter)
    assert [c[0] for c in adapter.executed_calls] == ["create_lead"]
    assert "limit" in _tool_messages(history)[0]["error"]


def test_write_limit_is_per_chat_id():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)
    lead = lambda: ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})
    run_turn([{"role": "user", "content": "a"}], adapter, _turn([lead()]), chat_id=1, write_limiter=limiter)
    run_turn([{"role": "user", "content": "b"}], adapter, _turn([lead()]), chat_id=2, write_limiter=limiter)
    assert len(adapter.executed_calls) == 2


def test_invalid_write_does_not_burn_the_limit():
    adapter = LeadAdapter({"lead_id": 1})
    limiter = FixedWindowRateLimiter(1, 3600)
    bad = ToolCall(name="create_lead", arguments={"name": "Camry"})
    good = ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"})
    run_turn([{"role": "user", "content": "a"}], adapter, _turn([bad]), chat_id=1, write_limiter=limiter)
    run_turn([{"role": "user", "content": "b"}], adapter, _turn([good]), chat_id=1, write_limiter=limiter)
    assert len(adapter.executed_calls) == 1


def test_reply_links_are_stripped():
    adapter = FakeCarsAdapter({"ok": True})
    result = run_turn(
        [{"role": "user", "content": "hi"}],
        adapter,
        _turn([ToolCall(name="search_inventory", arguments={})], final="Buy at https://evil.example/pay"),
    )
    assert "evil.example" not in result.reply


def test_reply_without_tool_call_is_also_filtered():
    llm = FakeLLMClient([LLMResponse(content="Go to www.evil.example now", tool_calls=[])])
    result = run_turn([{"role": "user", "content": "hi"}], FakeCarsAdapter({}), llm)
    assert "evil.example" not in result.reply


@pytest.mark.parametrize(
    "phrase",
    ["data, not instructions", "never reveal", "refuse", "never send links", "one or two friendly sentences", "same plain, polite voice"],
)
def test_system_prompt_carries_hardening_rules(phrase):
    assert phrase in SYSTEM_PROMPT.lower()


def test_blank_model_reply_after_a_tool_call_still_produces_a_reply():
    adapter = FakeCarsAdapter({"ok": True})
    result = run_turn(
        [{"role": "user", "content": "hi"}],
        adapter,
        _turn([ToolCall(name="search_inventory", arguments={})], final=""),
    )
    assert result.reply.strip()


def test_identical_write_calls_in_one_turn_execute_once():
    adapter = LeadAdapter({"lead_id": 42})
    limiter = FixedWindowRateLimiter(3, 3600)
    args = {"name": "Camry", "customer_name": "Sam"}
    calls = [ToolCall(name="create_lead", arguments=dict(args), id="a"), ToolCall(name="create_lead", arguments=dict(args), id="b")]
    history = [{"role": "user", "content": "book it"}]

    run_turn(history, adapter, _turn(calls), chat_id=1, write_limiter=limiter)

    assert [c[0] for c in adapter.executed_calls] == ["create_lead"]
    results = _tool_messages(history)
    assert len(results) == 2 and results[0] == results[1] == {"lead_id": 42}
    assert limiter.allow(1) and limiter.allow(1)  # only one of the three hits was spent


def test_different_write_calls_in_one_turn_both_execute():
    adapter = LeadAdapter({"lead_id": 1})
    calls = [
        ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": "Sam"}, id="a"),
        ToolCall(name="create_lead", arguments={"name": "Corolla", "customer_name": "Sam"}, id="b"),
    ]
    run_turn([{"role": "user", "content": "both"}], adapter, _turn(calls))
    assert len(adapter.executed_calls) == 2


# ---- review fixes: the model must see its own earlier replies -------------

def test_final_reply_is_appended_to_history_after_a_tool_call():
    adapter = FakeCarsAdapter({"ok": True})
    history = [{"role": "user", "content": "hi"}]
    result = run_turn(history, adapter, _turn([ToolCall(name="search_inventory", arguments={})], final="Here you go."))
    assert history[-1] == {"role": "assistant", "content": result.reply}


def test_final_reply_is_appended_to_history_without_a_tool_call():
    llm = FakeLLMClient([LLMResponse(content="Hello there", tool_calls=[])])
    history = [{"role": "user", "content": "hi"}]
    result = run_turn(history, FakeCarsAdapter({}), llm)
    assert history[-1] == {"role": "assistant", "content": result.reply} == {"role": "assistant", "content": "Hello there"}
