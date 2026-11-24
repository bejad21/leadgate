"""A model can send empty, truncated or non-object tool arguments, and a provider can answer HTTP 200
with an error body. None of that may crash the turn or stop the next model from being tried."""
import json

import httpx
import pytest
import respx

from engine.core.agent_loop import run_turn
from engine.llm_client import FailoverLLM, LLMClient, ProviderError, _parse_args
from engine.tests.test_agent_loop import FakeCarsAdapter

A_URL = "https://a.test/v1"
B_URL = "https://b.test/v1"


def completion(content=None, tool_calls=None):
    return {"choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}]}


def tool_call(arguments, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": "search_inventory", "arguments": arguments}}


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_arguments_mean_no_arguments(raw):
    assert _parse_args(raw) == {}


def test_a_dict_passes_through():
    assert _parse_args({"make": "Toyota"}) == {"make": "Toyota"}


def test_valid_json_is_parsed():
    assert _parse_args('{"make": "Toyota", "price_max": 20000}') == {"make": "Toyota", "price_max": 20000}


@pytest.mark.parametrize("raw", ['{"make": "Toy', "not json at all", "[1, 2]", "42"])
def test_unusable_arguments_come_back_as_a_marker_not_an_exception(raw):
    parsed = _parse_args(raw)

    assert list(parsed) == ["_invalid_arguments"]
    assert parsed["_invalid_arguments"] == raw[:200]


def test_the_marker_keeps_only_the_start_of_a_long_argument_string():
    assert len(_parse_args('{"a": "' + "x" * 1000)["_invalid_arguments"]) == 200


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "Provider returned error", "code": 502}},
        {"error": {"message": "rate limited"}, "choices": []},
        {},
        {"choices": []},
        {"choices": None},
    ],
)
def test_a_200_response_without_an_answer_is_a_provider_error(body):
    respx.post(f"{A_URL}/chat/completions").mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ProviderError):
        LLMClient("k", "model-a", base_url=A_URL).chat([], [])


@respx.mock
def test_a_200_response_that_is_not_json_is_a_provider_error():
    respx.post(f"{A_URL}/chat/completions").mock(return_value=httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(ProviderError):
        LLMClient("k", "model-a", base_url=A_URL).chat([], [])


@respx.mock
def test_failover_moves_to_the_next_model_when_a_200_carries_an_error():
    respx.post(f"{A_URL}/chat/completions").mock(
        return_value=httpx.Response(200, json={"error": {"message": "Provider returned error"}})
    )
    second = respx.post(f"{B_URL}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion(content="from the second model"))
    )
    llm = FailoverLLM([LLMClient("k", "model-a", base_url=A_URL), LLMClient("k", "model-b", base_url=B_URL)])

    assert llm.chat([], []).content == "from the second model"
    assert second.called


@respx.mock
def test_failover_raises_the_provider_error_when_every_model_does_that():
    for url in (A_URL, B_URL):
        respx.post(f"{url}/chat/completions").mock(return_value=httpx.Response(200, json={"error": {"message": "down"}}))
    llm = FailoverLLM([LLMClient("k", "model-a", base_url=A_URL), LLMClient("k", "model-b", base_url=B_URL)])

    with pytest.raises(ProviderError):
        llm.chat([], [])


@respx.mock
def test_a_tool_call_with_empty_arguments_still_runs():
    respx.post(f"{A_URL}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=completion(tool_calls=[tool_call("")])),
            httpx.Response(200, json=completion(content="Here is what we have.")),
        ]
    )
    adapter = FakeCarsAdapter({"matches": [], "count": 0})
    history = [{"role": "user", "content": "what cars do you have"}]

    result = run_turn(history, adapter, LLMClient("k", "model-a", base_url=A_URL))

    assert adapter.executed_calls == [("search_inventory", {})]
    assert result.reply == "Here is what we have."


@respx.mock
def test_truncated_arguments_become_a_tool_error_the_model_sees():
    seen_by_model = []

    def answer(request):
        seen_by_model.append(json.loads(request.content)["messages"])
        if len(seen_by_model) == 1:
            return httpx.Response(200, json=completion(tool_calls=[tool_call('{"make": "Toy')]))
        return httpx.Response(200, json=completion(content="Sorry, could you repeat that?"))

    respx.post(f"{A_URL}/chat/completions").mock(side_effect=answer)
    adapter = FakeCarsAdapter({"matches": [], "count": 0})
    history = [{"role": "user", "content": "a toyota please"}]

    result = run_turn(history, adapter, LLMClient("k", "model-a", base_url=A_URL))

    assert adapter.executed_calls == []
    assert result.reply == "Sorry, could you repeat that?"
    tool_message = next(m for m in seen_by_model[1] if m["role"] == "tool")
    assert "not valid JSON" in json.loads(tool_message["content"])["error"]
    assert tool_message["tool_call_id"] == "call_1"


@respx.mock
def test_a_tool_call_with_no_arguments_key_runs_like_one_with_empty_arguments():
    no_arguments = {"id": "call_1", "type": "function", "function": {"name": "search_inventory"}}
    respx.post(f"{A_URL}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=completion(tool_calls=[no_arguments])),
            httpx.Response(200, json=completion(content="Here is what we have.")),
        ]
    )
    adapter = FakeCarsAdapter({"matches": [], "count": 0})

    result = run_turn([{"role": "user", "content": "what cars do you have"}], adapter, LLMClient("k", "model-a", base_url=A_URL))

    assert adapter.executed_calls == [("search_inventory", {})]
    assert result.reply == "Here is what we have."
