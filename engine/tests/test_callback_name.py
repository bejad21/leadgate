"""A customer who asks to be called and has not said who they are is asked for their name (fresh 6 test, p-r089: "Can
an agent call me? My number is 860-555-0150" was answered with a question about the property only)."""
import pytest

from engine.core.agent_loop import ASK_NAME_TOO, run_turn
from engine.llm_client import LLMResponse
from engine.tests.test_agent_habits import ShopAdapter, calls, says
from engine.tests.test_agent_loop import FakeLLMClient

WHICH = "I'd be happy to have someone reach out! Which property or listing are you interested in?"


def reply_to(*user_texts, model_says=WHICH):
    history = []
    for text in user_texts[:-1]:
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": "Sure."}]
    history.append({"role": "user", "content": user_texts[-1]})
    return run_turn(history, ShopAdapter(), FakeLLMClient([says(model_says)])).reply


def test_the_name_is_asked_for_when_the_reply_forgot_it():
    assert reply_to("Can an agent call me? My number is 860-555-0150") == f"{WHICH} {ASK_NAME_TOO}"


@pytest.mark.parametrize("text", [
    "please have someone call me, 513 555 0188",
    "call me back on 513 555 0188",
    "Could somebody phone me about the Camry?",
])
def test_other_ways_of_asking_for_a_call(text):
    assert reply_to(text).endswith(ASK_NAME_TOO), text


@pytest.mark.parametrize("texts", [
    ("Can an agent call me? I'm Dana Reyes, 860-555-0150",),
    ("My name is Omar", "have someone call me on 860-555-0150"),
    ("Hugo Marsh, 513 555 0188, call me please",),
    ("this is Mia. Can someone call me?",),
    ("Sam, 860-555-0150. Call me.",),
])
def test_a_customer_who_gave_a_name_is_not_asked_again(texts):
    assert reply_to(*texts) == WHICH, texts


def test_a_reply_that_already_asks_for_the_name_is_left_alone():
    asked = "Happy to. What is your name, and which property is it?"
    assert reply_to("Can an agent call me? My number is 860-555-0150", model_says=asked) == asked


def test_a_message_that_does_not_ask_for_a_call_is_left_alone():
    assert reply_to("do you have any Toyota?") == WHICH


def test_a_turn_that_placed_a_request_is_left_alone():
    llm = FakeLLMClient([
        calls(("search_inventory", {})),
        calls(("reserve_item", {"item_id": 7, "customer_name": "Sam", "customer_contact": "860-555-0150"})),
        LLMResponse(content="Passed on. Someone will call you.", tool_calls=[]),
    ])
    result = run_turn([{"role": "user", "content": "Sam here, hold the Camry and call me on 860-555-0150"}], ShopAdapter(), llm)
    assert ASK_NAME_TOO not in result.reply
