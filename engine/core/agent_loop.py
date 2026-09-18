import json
import uuid
from dataclasses import dataclass, field
from engine.llm_client import LLMClient, ToolCall
from engine.core.adapter_base import DomainAdapter

@dataclass
class AgentTurnResult:
    reply: str
    tool_calls_made: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)

# System prompt injected as the first message of every conversation.
#
# Deliberately domain-agnostic (Global Constraint: no car/real-estate-specific
# strings): it describes the general decision rule for when to search a
# catalog versus when to create a CRM lead, not any particular vertical's
# vocabulary. This exists because, without any system prompt at all, the
# model defaults to calling the search tool even when the customer has
# already named the specific item they want and clearly intends to buy it --
# see Task 5.2 eval failures (cars indices 37/38/40/41/42, real_estate
# indices 38-45) where "I want to buy <specific item> at <price>, here's my
# name/contact" still triggered another search instead of create_lead.
SYSTEM_PROMPT = (
    "You are a sales assistant helping a customer browse a catalog of items "
    "and, when appropriate, hand them off to a human by creating a CRM lead.\n\n"
    "Decision rule for choosing which tool to call:\n"
    "- If the customer has ALREADY identified a specific item they want "
    "(by name, price, or other identifying detail already present in "
    "their message) AND has given contact information (a name, email, or "
    "phone number) AND has expressed clear intent to buy or move forward "
    "with that item, call the lead-creation tool directly. Do not search "
    "again first -- the customer already told you which item they want, so "
    "another search would just stall them.\n"
    "- Otherwise (they are still exploring, have not named a specific item, "
    "or have not given contact info / purchase intent yet), use the search "
    "tool to help them find items matching what they described.\n"
    "- Apply this rule even if you are not 100% certain of every detail: if "
    "the item and contact info are both already in the conversation, call "
    "the lead-creation tool now rather than searching first to double-check.\n\n"
    "Never fabricate details that were not given to you or returned by a "
    "tool. In particular, never write out what looks like a tool call or a "
    "tool's raw result as plain text in your reply -- only a real tool call "
    "you actually make produces a real result. If the search results you "
    "already received don't match what the customer asked for (for example "
    "they asked for something your search tool has no field to filter on), "
    "say so honestly using only the real items you were actually given, or "
    "ask a clarifying question in plain language -- do not invent "
    "additional items, prices, or a second search result to fill the gap."
)

def run_turn(history: list[dict], adapter: DomainAdapter, llm: LLMClient) -> AgentTurnResult:
    if not history or history[0].get("role") != "system":
        history.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    response = llm.chat(history, tools=adapter.tool_schemas())
    tool_results = []

    if response.tool_calls:
        # The OpenAI/OpenRouter tool-calling protocol requires the
        # assistant's own message (carrying its `tool_calls` array, each
        # with an `id`) to be appended to history BEFORE any `tool` role
        # messages, and each `tool` message must carry a `tool_call_id`
        # that matches one of those ids. Skipping the assistant message
        # and omitting tool_call_id (as this used to do) happens to work
        # against the currently-configured free-tier model, but is a real
        # protocol violation that could break silently if the model/
        # provider ever changes -- a real risk given this project's own
        # free-tier constraint (see config.py's provider fallback logic).
        assistant_tool_calls = []
        for call in response.tool_calls:
            call_id = call.id or f"call_{uuid.uuid4().hex}"
            call.id = call_id
            assistant_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
            )
        history.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": assistant_tool_calls,
            }
        )

        for call in response.tool_calls:
            result = adapter.execute_tool(call.name, call.arguments)
            tool_results.append(result)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result),
                }
            )

    if response.tool_calls:
        # Ask the LLM to turn tool results into a natural-language reply, grounded only in those results
        follow_up = llm.chat(history, tools=[])
        reply = follow_up.content or ""
    else:
        reply = response.content or ""

    return AgentTurnResult(reply=reply, tool_calls_made=response.tool_calls, tool_results=tool_results)
