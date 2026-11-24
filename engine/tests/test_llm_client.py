import json

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
    # The tool_call id must be preserved: the OpenAI/OpenRouter protocol uses it to link tool results back.
    assert call.id == "call_1"


def _post_once(message: dict, temperature: float = 0.0):
    from engine.llm_client import Sampling

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": message}]})
    )
    client = LLMClient(api_key="k", model="m", sampling=Sampling(temperature=temperature))
    result = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    return route, result


@respx.mock
def test_greedy_sampling_sends_top_p_one():
    route, _ = _post_once({"content": "ok"}, temperature=0.0)
    assert json.loads(route.calls[0].request.content)["top_p"] == 1


@respx.mock
def test_warm_sampling_does_not_send_top_p():
    route, _ = _post_once({"content": "ok"}, temperature=0.7)
    assert "top_p" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_content_blocks_keep_text_and_drop_thinking():
    blocks = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "private reasoning"}], "closed": True},
        {"type": "text", "text": "Hello "},
        {"type": "text", "text": "there"},
    ]
    _, result = _post_once({"content": blocks})
    assert result.content == "Hello there"


@respx.mock
def test_content_blocks_with_only_thinking_become_none():
    blocks = [{"type": "thinking", "thinking": [{"type": "text", "text": "x"}], "closed": True}]
    _, result = _post_once({"content": blocks})
    assert result.content is None


@respx.mock
def test_reasoning_effort_is_sent_only_when_set():
    from engine.llm_client import Sampling

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    client = LLMClient(api_key="k", model="m", sampling=Sampling(reasoning_effort="low"))
    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert json.loads(route.calls[0].request.content)["reasoning_effort"] == "low"
    LLMClient(api_key="k", model="m", sampling=Sampling()).chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in json.loads(route.calls[1].request.content)


def test_reasoning_effort_from_env(monkeypatch):
    import pytest

    from engine.llm_client import Sampling

    monkeypatch.setenv("LLM_REASONING_EFFORT", "medium")
    assert Sampling.from_env().reasoning_effort == "medium"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        Sampling.from_env()
    monkeypatch.delenv("LLM_REASONING_EFFORT")
    assert Sampling.from_env().reasoning_effort is None


@respx.mock
def test_an_empty_tool_list_is_left_out_of_the_request():
    route, _ = _post_once({"content": "ok"})  # chat() is called with tools=[]
    assert "tools" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_a_tool_list_is_sent_as_given():
    from engine.llm_client import Sampling

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}]
    LLMClient(api_key="k", model="m", sampling=Sampling()).chat(messages=[{"role": "user", "content": "hi"}], tools=tools)
    assert json.loads(route.calls[0].request.content)["tools"] == tools


@respx.mock
def test_tool_choice_is_sent_only_when_asked_for_and_only_with_tools():
    from engine.llm_client import Sampling

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}]
    client = LLMClient(api_key="k", model="m", sampling=Sampling())
    client.chat_requiring_a_tool([{"role": "user", "content": "hi"}], tools)
    client.chat([{"role": "user", "content": "hi"}], tools)
    client.chat_requiring_a_tool([{"role": "user", "content": "hi"}], [])
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[0]["tool_choice"] == "required"
    assert "tool_choice" not in bodies[1] and "tool_choice" not in bodies[2]


def test_the_call_timeout_is_30_seconds_unless_set(monkeypatch):
    import pytest

    from engine.llm_client import Sampling

    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    assert Sampling.from_env().timeout_seconds == 30.0
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    assert Sampling.from_env().timeout_seconds == 120.0
    for bad in ("0", "601", "soon", "nan"):
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", bad)
        with pytest.raises(ValueError, match="LLM_TIMEOUT_SECONDS"):
            Sampling.from_env()


def test_the_configured_timeout_reaches_the_request(monkeypatch):
    from engine.llm_client import Sampling

    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    LLMClient(api_key="k", model="m", sampling=Sampling(timeout_seconds=120)).chat([{"role": "user", "content": "hi"}], [])
    assert seen["timeout"] == 120
