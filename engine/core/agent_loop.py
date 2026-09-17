from dataclasses import dataclass, field
from engine.llm_client import LLMClient, ToolCall
from engine.core.adapter_base import DomainAdapter

@dataclass
class AgentTurnResult:
    reply: str
    tool_calls_made: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)

def run_turn(history: list[dict], adapter: DomainAdapter, llm: LLMClient) -> AgentTurnResult:
    response = llm.chat(history, tools=adapter.tool_schemas())
    tool_results = []
    for call in response.tool_calls:
        result = adapter.execute_tool(call.name, call.arguments)
        tool_results.append(result)
        history.append({"role": "tool", "name": call.name, "content": str(result)})

    if response.tool_calls:
        # Ask the LLM to turn tool results into a natural-language reply, grounded only in those results
        follow_up = llm.chat(history, tools=[])
        reply = follow_up.content or ""
    else:
        reply = response.content or ""

    return AgentTurnResult(reply=reply, tool_calls_made=response.tool_calls, tool_results=tool_results)
