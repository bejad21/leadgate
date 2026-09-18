import httpx
import respx

from engine.llm_client import LLMClient, ToolCall


@respx.mock
def test_chat_parses_tool_call():
    canned_response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_odoo_catalog",
                                "arguments": '{"make": "Toyota", "max_price": 20000}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=canned_response)
    )

    client = LLMClient(api_key="test-key", model="test-model")
    result = client.chat(
        messages=[{"role": "user", "content": "find me a cheap Toyota"}],
        tools=[{"type": "function", "function": {"name": "search_odoo_catalog"}}],
    )

    assert result.content is None
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == "search_odoo_catalog"
    assert call.arguments == {"make": "Toyota", "max_price": 20000}
    # The real API response's tool_call id must be preserved, not dropped --
    # it's required downstream to link the assistant's tool_calls to the
    # matching tool-result message per the OpenAI/OpenRouter protocol.
    assert call.id == "call_1"
