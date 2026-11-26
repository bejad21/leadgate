"""An availability question that names a place is searched with that place, even when the model leaves it out (fresh 6
test, p-r020: "has the Eliot house at 850k been sold already?" was searched by price only, so a sold listing would not
have been reported)."""
import json

from engine.adapters.real_estate import RealEstateAdapter
from engine.core.agent_loop import run_turn
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.fakes import FakeOdoo, item
from engine.tests.test_agent_loop import FakeLLMClient


def home(item_id, city, price, status="available", state="Maine", beds="5"):
    row = item(item_id, name=f"{beds}bd/{beds}ba residential - {city}, {state}", price=price, status=status, domain_type="real_estate")
    row["attributes"] = json.dumps({"city": city, "state": state, "bed": beds, "bath": beds})
    return row


class PlaceOdoo(FakeOdoo):
    """FakeOdoo that also applies the name filter a location search uses."""

    def search_read(self, model, domain, fields, **kwargs):
        rows = super().search_read(model, domain, fields, **kwargs)
        for clause in domain:
            if isinstance(clause, tuple) and clause[0] == "name" and clause[1] == "ilike":
                rows = [row for row in rows if str(clause[2]).lower() in str(row["name"]).lower()]
        return rows


def adapter():
    rows = [home(1, "Eliot", 850000.0, status="sold"), home(2, "York", 850000.0), home(3, "New York", 900000.0, state="New York")]
    return RealEstateAdapter(PlaceOdoo(items={r["id"]: r for r in rows}))


def search_by_price():
    return LLMResponse(content=None, tool_calls=[ToolCall(name="search_listings", arguments={"price_min": 850000, "price_max": 850000}, id="s1")])


def ask(text):
    llm = FakeLLMClient([search_by_price(), LLMResponse(content="Here is what I found.", tool_calls=[])])
    return run_turn([{"role": "user", "content": text}], adapter(), llm)


def test_the_place_named_in_an_availability_question_is_added_to_the_search():
    result = ask("has the Eliot house at 850k been sold already?")
    assert result.tool_calls_made[0].arguments["location"] == "Eliot"
    assert result.tool_results[0]["unavailable"] == [{"name": "5bd/5ba residential - Eliot, Maine", "status": "sold"}]


def test_a_lower_case_place_next_to_the_word_house_counts():
    assert ask("is the eliot house still for sale?").tool_calls_made[0].arguments["location"] == "Eliot"


def test_the_longer_place_name_wins_over_the_one_inside_it():
    assert ask("is the New York house still available?").tool_calls_made[0].arguments["location"] == "New York"


def test_a_search_that_already_names_a_place_is_left_alone():
    homes = adapter()
    given = {"location": "York", "price_max": 850000}
    assert homes.complete_search("search_listings", given, "is the Eliot house still available?") == given


def test_no_place_or_two_places_adds_nothing():
    homes = adapter()
    for text in ("is it still available?", "is the Eliot house or the York house still available?"):
        assert homes.complete_search("search_listings", {"price_max": 1}, text) == {"price_max": 1}, text


def test_a_question_that_is_not_about_availability_is_left_to_the_model():
    assert "location" not in ask("what do you have around 850k? I moved here from Eliot").tool_calls_made[0].arguments


def test_a_catalog_that_cannot_be_read_leaves_the_search_as_it_was():
    class Down:
        def search_read(self, *args, **kwargs):
            raise RuntimeError("odoo is down")

    assert RealEstateAdapter(Down()).complete_search("search_listings", {}, "is the Eliot house still available?") == {}
