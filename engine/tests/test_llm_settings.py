"""Sampling settings, the model that answered, token usage and cost: every call sends the settings,
and every response says which model produced it and what it cost."""
import json

import httpx
import pytest
import respx

from engine.llm_client import LLMClient, Sampling, Usage, estimate_cost

URL = "https://openrouter.ai/api/v1/chat/completions"


def _ok(**extra):
    body = {"choices": [{"message": {"content": "hi", "tool_calls": []}}], **extra}
    return httpx.Response(200, json=body)


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    for name in ("LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_SEED", "LLM_PRICE_PER_MTOK_INPUT", "LLM_PRICE_PER_MTOK_OUTPUT"):
        monkeypatch.delenv(name, raising=False)


@respx.mock
def test_defaults_are_sent_on_every_call_and_no_seed():
    route = respx.post(URL).mock(return_value=_ok())
    LLMClient(api_key="k", model="m").chat([{"role": "user", "content": "x"}], [])
    body = _sent(route)
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 800
    assert "seed" not in body


@respx.mock
def test_settings_come_from_the_environment_when_the_client_is_built(monkeypatch):
    route = respx.post(URL).mock(return_value=_ok())
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_MAX_TOKENS", "256")
    monkeypatch.setenv("LLM_SEED", "42")
    client = LLMClient(api_key="k", model="m")
    monkeypatch.setenv("LLM_TEMPERATURE", "1.5")  # changed after construction: not picked up
    client.chat([], [])
    body = _sent(route)
    assert (body["temperature"], body["max_tokens"], body["seed"]) == (0.7, 256, 42)


@respx.mock
def test_blank_values_mean_the_defaults(monkeypatch):
    route = respx.post(URL).mock(return_value=_ok())
    for name in ("LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_SEED"):
        monkeypatch.setenv(name, "  ")
    LLMClient(api_key="k", model="m").chat([], [])
    body = _sent(route)
    assert (body["temperature"], body["max_tokens"]) == (0.0, 800) and "seed" not in body


@respx.mock
def test_the_model_usage_and_latency_are_reported():
    respx.post(URL).mock(return_value=_ok(usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}))
    result = LLMClient(api_key="k", model="vendor/model-a").chat([], [])
    assert result.model == "vendor/model-a"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens) == (120, 30, 150)
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0


@respx.mock
def test_a_provider_that_omits_usage_gives_zeros():
    respx.post(URL).mock(return_value=_ok())
    usage = LLMClient(api_key="k", model="m").chat([], []).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 0, 0)
    assert usage.cost == 0.0


@pytest.mark.parametrize(
    "junk",
    [None, "lots", [], 7, {"prompt_tokens": "many", "completion_tokens": None, "total_tokens": -5}, {"prompt_tokens": True}],
)
@respx.mock
def test_unusable_usage_never_raises(junk):
    respx.post(URL).mock(return_value=_ok(usage=junk))
    usage = LLMClient(api_key="k", model="m").chat([], []).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 0, 0)


@respx.mock
def test_total_tokens_is_derived_when_the_provider_leaves_it_out():
    respx.post(URL).mock(return_value=_ok(usage={"prompt_tokens": 10, "completion_tokens": 5}))
    assert LLMClient(api_key="k", model="m").chat([], []).usage.total_tokens == 15


@respx.mock
def test_a_cost_the_provider_reports_is_used(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_PER_MTOK_INPUT", "1000")
    respx.post(URL).mock(return_value=_ok(usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.00042}))
    assert LLMClient(api_key="k", model="m").chat([], []).usage.cost == pytest.approx(0.00042)


@respx.mock
def test_without_a_reported_cost_it_is_computed_from_the_prices(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_PER_MTOK_INPUT", "2")
    monkeypatch.setenv("LLM_PRICE_PER_MTOK_OUTPUT", "10")
    respx.post(URL).mock(return_value=_ok(usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000}))
    assert LLMClient(api_key="k", model="m").chat([], []).usage.cost == pytest.approx(7.0)


def test_estimate_cost_is_a_pure_function():
    usage = Usage(prompt_tokens=2000, completion_tokens=1000, total_tokens=3000)
    assert estimate_cost(usage, price_input=3.0, price_output=15.0) == pytest.approx(0.021)
    assert estimate_cost(usage, price_input=0.0, price_output=0.0) == 0.0
    assert estimate_cost(Usage(cost=0.5), price_input=3.0, price_output=15.0) == 0.5  # the provider's figure wins
    assert estimate_cost(Usage(), price_input=3.0, price_output=15.0) == 0.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_TEMPERATURE", "warm"),
        ("LLM_TEMPERATURE", "nan"),
        ("LLM_TEMPERATURE", "inf"),
        ("LLM_TEMPERATURE", "-0.1"),
        ("LLM_TEMPERATURE", "2.5"),
        ("LLM_MAX_TOKENS", "0"),
        ("LLM_MAX_TOKENS", "-5"),
        ("LLM_MAX_TOKENS", "lots"),
        ("LLM_MAX_TOKENS", "1.5"),
        ("LLM_MAX_TOKENS", "10000000"),
        ("LLM_SEED", "abc"),
        ("LLM_SEED", "1.5"),
        ("LLM_PRICE_PER_MTOK_INPUT", "-1"),
        ("LLM_PRICE_PER_MTOK_INPUT", "nan"),
        ("LLM_PRICE_PER_MTOK_OUTPUT", "inf"),
        ("LLM_PRICE_PER_MTOK_OUTPUT", "cheap"),
    ],
)
def test_a_bad_value_is_refused_and_the_error_names_the_variable(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        LLMClient(api_key="k", model="m")


def test_explicit_sampling_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "bad")
    client = LLMClient(api_key="k", model="m", sampling=Sampling(temperature=0.3, max_tokens=100))
    assert client.sampling.temperature == 0.3
