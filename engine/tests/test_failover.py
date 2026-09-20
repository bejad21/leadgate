"""Free model catalogs rotate: a model that worked yesterday can 404 today. The engine walks an ordered
list of models and remembers, for a while, which ones are down, so one retirement does not take the
assistant with it."""
import httpx
import pytest

from engine.llm_client import FailoverLLM, LLMResponse


def status_error(code):
    request = httpx.Request("POST", "https://example.test/chat/completions")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


class Model:
    def __init__(self, name, outcome):
        self.model, self.outcome, self.calls = name, outcome, 0

    def chat(self, messages, tools):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


OK = LLMResponse(content="hello", tool_calls=[])


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_first_working_model_answers_and_the_rest_are_not_called():
    first, second = Model("a", OK), Model("b", OK)
    assert FailoverLLM([first, second]).chat([], []).content == "hello"
    assert (first.calls, second.calls) == (1, 0)


@pytest.mark.parametrize("code", [404, 429, 500, 502, 503])
def test_a_retired_or_overloaded_model_hands_over_to_the_next(code):
    down, up = Model("a", status_error(code)), Model("b", OK)
    assert FailoverLLM([down, up]).chat([], []).content == "hello"
    assert (down.calls, up.calls) == (1, 1)


def test_a_timeout_or_network_error_hands_over_too():
    down, up = Model("a", httpx.ConnectTimeout("slow")), Model("b", OK)
    assert FailoverLLM([down, up]).chat([], []).content == "hello"


@pytest.mark.parametrize("code", [400, 401, 403])
def test_a_request_the_provider_rejects_is_not_retried_elsewhere(code):
    """A bad request or a bad key is our fault, and hiding it behind another model would mask it."""
    first, second = Model("a", status_error(code)), Model("b", OK)
    with pytest.raises(httpx.HTTPStatusError):
        FailoverLLM([first, second]).chat([], [])
    assert second.calls == 0


def test_when_every_model_is_down_the_last_error_is_raised():
    llm = FailoverLLM([Model("a", status_error(404)), Model("b", status_error(429))])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        llm.chat([], [])
    assert caught.value.response.status_code == 429


def test_a_model_that_failed_is_skipped_until_the_cooldown_passes():
    clock = Clock()
    down, up = Model("a", status_error(404)), Model("b", OK)
    llm = FailoverLLM([down, up], cooldown_seconds=600, clock=clock)
    llm.chat([], [])
    llm.chat([], [])
    assert down.calls == 1  # not asked again straight away
    clock.now += 601
    llm.chat([], [])
    assert down.calls == 2  # given another chance


def test_a_recovered_model_is_used_again_after_the_cooldown():
    clock = Clock()
    flaky = Model("a", status_error(429))
    up = Model("b", OK)
    llm = FailoverLLM([flaky, up], cooldown_seconds=60, clock=clock)
    llm.chat([], [])
    flaky.outcome = OK
    clock.now += 61
    llm.chat([], [])
    assert flaky.calls == 2 and up.calls == 1


def test_if_everything_is_in_cooldown_the_models_are_tried_anyway():
    clock = Clock()
    a, b = Model("a", status_error(429)), Model("b", status_error(429))
    llm = FailoverLLM([a, b], clock=clock)
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat([], [])
    a.outcome = OK
    assert llm.chat([], []).content == "hello"  # waiting out a cooldown would only make things worse


def test_it_needs_at_least_one_model():
    with pytest.raises(ValueError):
        FailoverLLM([])
