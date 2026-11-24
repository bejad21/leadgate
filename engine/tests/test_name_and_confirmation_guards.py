import json

from engine.core import agent_loop as loop
from engine.core.agent_loop import claims_confirmed, customer_gave_name, request_passed_on_reply
from engine.llm_client import ToolCall


def lead(name):
    return ToolCall(name="create_lead", arguments={"name": "Camry", "customer_name": name})


def test_a_name_the_customer_wrote_is_accepted():
    assert customer_gave_name(lead("Dana Reyes"), "I'm Dana Reyes, dana@example.com")


def test_a_name_made_from_the_phone_number_is_refused():
    assert not customer_gave_name(lead("937-555-0121"), "Lead for the GV70 please, my number is 937-555-0121.")


def test_a_name_guessed_from_the_email_address_is_refused():
    assert not customer_gave_name(lead("Tobias K"), "tobias.k@example.com - interested in the Toms River house")


def test_accents_and_case_do_not_matter():
    assert customer_gave_name(lead("Jose Garcia"), "JOSÉ GARCIA here, 555 010 2020")


def test_a_name_from_an_earlier_message_counts():
    assert customer_gave_name(lead("Mia"), "hello | I am Mia | put me down, mia@example.com")


def test_calls_without_a_customer_name_argument_are_not_judged():
    assert customer_gave_name(ToolCall(name="search_inventory", arguments={"make": "Ford"}), "ford?")


def test_confirmation_wording_is_found():
    assert claims_confirmed("You're all set to request a viewing, Zoe!")
    assert claims_confirmed("Your viewing is confirmed for Saturday.")
    assert not claims_confirmed("A person will confirm the time with you.")


def test_the_replacement_reply_says_it_is_a_request():
    result = {"lead_id": 1, "kind": "viewing", "item_name": "2021 Toyota 4Runner", "detail": "Sat 14 Nov, morning"}
    text = request_passed_on_reply([result]) or ""
    assert "viewing request" in text and "2021 Toyota 4Runner" in text and "not a confirmation" in text
    assert request_passed_on_reply([{"error": "nope"}]) is None


def test_the_loop_exposes_two_extra_rounds():
    assert loop.EXTRA_TOOL_ROUNDS == 2
    assert json.dumps(loop.NO_NAME_GIVEN)
