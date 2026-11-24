"""A viewing is booked only for a day and a time of day the customer said. The model must
ask instead of choosing a slot for them."""

import pytest

from engine.core.agent_loop import customer_gave_day_and_time, run_turn
from engine.core.tools import BookViewingArgs, Tool
from engine.llm_client import ToolCall
from engine.tests.test_agent_loop import FakeLLMClient, RetryAdapter, _calls, _say
from engine.tests.test_agent_two_step import FOUND, _search


class ViewingAdapter(RetryAdapter):
    def tools(self):
        return [*super().tools(), Tool("book_viewing", "View", BookViewingArgs)]


def _book(slot="afternoon", date="2026-11-12"):
    return ToolCall(
        name="book_viewing",
        arguments={"item_id": 4, "customer_name": "Dina", "date": date, "slot": slot},
        id="v1",
    )


@pytest.mark.parametrize(
    "text",
    [
        "I'd like to see it tomorrow morning",
        "can I come Saturday afternoon?",
        "this saturday at 3pm please",
        "next Tuesday evening works",
        "on 2026-11-14 in the morning",
        "11/14 around 10 am",
        "tonight",
        "today after noon",
        "Ich möchte den Wagen am Dienstag Nachmittag ansehen",
        "Gostaria de visitar a casa na quinta-feira a tarde",
        "Quiero verlo mañana por la tarde",
        "Je voudrais le voir samedi matin",
        "Marhaba, ureed ziyarat bayt yawm as-sabt sabahan",
        "ghadan zuhran in sha Allah",
    ],
)
def test_a_day_and_a_time_of_day_are_recognised(text):
    assert customer_gave_day_and_time(text)


@pytest.mark.parametrize(
    "text",
    [
        "I want to see the Mullica Hill house at 929000 tomorrow. Dina Park, dina@example.com",
        "I'd like to see it. Dina Park, 513-555-0100",
        "can I come in the morning?",
        "Saturday works",
        "Ich möchte den Wagen am Dienstag ansehen",
        "Quiero verlo mañana",
        "",
    ],
)
def test_a_missing_day_or_time_is_caught(text):
    assert not customer_gave_day_and_time(text)


def test_no_viewing_is_booked_when_the_customer_gave_no_time_of_day():
    adapter = ViewingAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _calls(_book()), _say("Which time of day suits you?")])

    result = run_turn([{"role": "user", "content": "see the Camry tomorrow. Dina, dina@example.com"}], adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory"]
    assert "time of day" in result.tool_results[1]["error"]
    assert result.reply == "Which time of day suits you?"


def test_a_viewing_is_booked_when_the_customer_gave_both():
    adapter = ViewingAdapter(FOUND)
    llm = FakeLLMClient([_calls(_search()), _calls(_book("morning")), _say("Requested.")])

    result = run_turn(
        [{"role": "user", "content": "see the Camry tomorrow morning. Dina, dina@example.com"}], adapter, llm, chat_id=1
    )

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "book_viewing"]
    assert "error" not in result.tool_results[1]


def test_the_day_and_time_can_come_from_an_earlier_message():
    adapter = ViewingAdapter(FOUND)
    history = [
        {"role": "user", "content": "can I see the Camry on Saturday morning?"},
        {"role": "assistant", "content": "Sure, which car and what is your name and number?"},
        {"role": "user", "content": "the 2024 one, Dina, dina@example.com"},
    ]
    llm = FakeLLMClient([_calls(_search()), _calls(_book("morning", "2026-11-14")), _say("Requested.")])

    run_turn(history, adapter, llm, chat_id=1)

    assert [name for name, _ in adapter.executed_calls] == ["search_inventory", "book_viewing"]


@pytest.mark.parametrize(
    "reply",
    [
        "Ihre Anfrage für eine Besichtigung wurde weitergeleitet.",
        "Anotei seu pedido de visita para quinta-feira à tarde.",
        "Passei o seu pedido de visita para quinta-feira.",
        "Pasé su solicitud al equipo.",
        "J'ai transmis votre demande à l'équipe.",
    ],
)
def test_a_passed_on_claim_is_recognised_in_other_languages(reply):
    from engine.core.agent_loop import claims_passed_on

    assert claims_passed_on(reply)
