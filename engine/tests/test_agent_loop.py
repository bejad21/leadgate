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
                "function": {"name": "search_inventory", "parameters": {}},
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
