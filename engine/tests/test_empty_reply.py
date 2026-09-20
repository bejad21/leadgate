"""The model sometimes returns nothing after a tool call. A customer should not be told "I didn't
catch that" for a request that worked, so the loop asks once more before giving up."""
from engine.core.agent_loop import run_turn
from engine.core.guardrails import EMPTY_REPLY
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.test_hardening import ActionAdapter


class Sequence:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        return self._responses.pop(0)


SEARCH = LLMResponse(content=None, tool_calls=[ToolCall(name="search_inventory", arguments={})])
history = lambda: [{"role": "user", "content": "show me cars"}]


def test_an_empty_answer_after_a_tool_call_is_asked_for_again():
    llm = Sequence(SEARCH, LLMResponse(content="", tool_calls=[]), LLMResponse(content="Here are three cars.", tool_calls=[]))
    assert run_turn(history(), ActionAdapter(), llm).reply == "Here are three cars."
    assert llm.calls == 3


def test_a_whitespace_only_answer_counts_as_empty():
    llm = Sequence(SEARCH, LLMResponse(content="  \n ", tool_calls=[]), LLMResponse(content="Found some.", tool_calls=[]))
    assert run_turn(history(), ActionAdapter(), llm).reply == "Found some."


def test_two_empty_answers_fall_back_to_the_polite_reply():
    llm = Sequence(SEARCH, LLMResponse(content="", tool_calls=[]), LLMResponse(content=None, tool_calls=[]))
    assert run_turn(history(), ActionAdapter(), llm).reply == EMPTY_REPLY
    assert llm.calls == 3  # one retry, not a loop


def test_a_good_first_answer_is_not_retried():
    llm = Sequence(SEARCH, LLMResponse(content="Found some.", tool_calls=[]))
    run_turn(history(), ActionAdapter(), llm)
    assert llm.calls == 2


def test_a_reply_with_no_tool_call_is_untouched():
    llm = Sequence(LLMResponse(content="Hello!", tool_calls=[]))
    assert run_turn(history(), ActionAdapter(), llm).reply == "Hello!"
    assert llm.calls == 1
