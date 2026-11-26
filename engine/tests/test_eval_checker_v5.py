"""Checker v5: a town written with an abbreviation ("Ft. Thomas") was read as the word after the dot, so a place that
was in the results was reported as made up (fresh 6 test, p-c010). A place that is not in the results must still fail."""
import json

from eval import claims


def _results(*places):
    rows = [
        {"id": n, "name": "2026 Chevrolet Traverse", "price": 37110.0, "attributes": json.dumps({"location": place})}
        for n, place in enumerate(places, 1)
    ]
    return [{"matches": rows, "count": len(rows)}]


def _places(reply, *places):
    found = claims.check_reply(reply, tool_results=_results(*places), tool_calls=[])
    return {c["text"]: c["grounded"] for c in found if c["type"] == "location"}


def test_a_town_with_an_abbreviation_that_is_in_the_results_is_grounded():
    for place in ("Ft. Thomas, KY", "St. Petersburg, FL", "Mt. Vernon, OH", "Port St. Lucie, FL"):
        verdicts = _places(f"One is in {place}.", place)
        assert verdicts and all(verdicts.values()), (place, verdicts)


def test_a_town_with_an_abbreviation_that_is_not_in_the_results_is_still_made_up():
    assert not all(_places("One is in Ft. Myers, FL.", "Ft. Thomas, KY").values())
    assert not all(_places("One is in St. Louis, MO.", "St. Petersburg, FL").values())


def test_the_end_of_a_sentence_is_not_read_as_part_of_a_town():
    verdicts = _places("I only checked Main St. Dayton, OH is where it sits.", "Dayton, OH")
    assert verdicts and all(verdicts.values()), verdicts
