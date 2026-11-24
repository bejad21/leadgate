import json

import pytest

from eval import claims


def car(id_, price, **attrs):
    base = {"make": "Toyota", "model": "Camry", "year": "2020", "mileage": "41000", "condition": "Used", "location": "Austin, TX"}
    base.update(attrs)
    return {"id": id_, "name": f"{base['year']} {base['make']} {base['model']}", "price": float(price), "attributes": json.dumps(base)}


def home(id_, price, bed="3", bath="2", city="Salem", state="Massachusetts", size="1500"):
    return {
        "id": id_, "name": f"{bed}bd/{bath}ba residential - {city}, {state}", "price": float(price),
        "attributes": json.dumps({"bed": bed, "bath": bath, "city": city, "state": state, "house_size": size}),
    }


def check(reply, rows=(), message="", counts=None, calls=()):
    results = [{"matches": list(rows), "count": len(rows) if counts is None else counts}]
    return claims.check_reply(reply, tool_results=results, tool_calls=list(calls), context_text=message)


def ungrounded(verdicts):
    return [v for v in verdicts if not v["grounded"]]


def test_a_price_from_the_results_is_grounded():
    verdicts = check("The 2020 Camry is $21,834.", [car(1, 21834)])
    assert [v["type"] for v in verdicts if v["type"] == "price"] == ["price"]
    assert not ungrounded(verdicts)


def test_an_invented_price_is_ungrounded():
    bad = ungrounded(check("The 2020 Camry is $19,999.", [car(1, 21834)]))
    assert [(v["type"], v["value"]) for v in bad] == [("price", 19999.0)]


def test_a_price_inside_a_suggestion_sentence_is_still_checked():
    bad = ungrounded(check("Would you like me to narrow it to $18,000 to $19,000?", [car(1, 21834)]))
    assert {v["value"] for v in bad} == {18000.0, 19000.0}


def test_a_figure_the_customer_gave_is_not_a_claim_about_the_results():
    assert not ungrounded(check("Nothing under $15,000, sorry.", [], message="anything under $15,000?", counts=0))


def test_shorthand_prices_may_round_to_their_precision_only():
    rows = [car(1, 249900)]
    assert not ungrounded(check("A $250k car.", rows))
    assert ungrounded(check("A $260k car.", rows))
    assert ungrounded(check("A $250,000 car.", rows))


def test_the_ceiling_the_agent_searched_with_is_allowed():
    calls = [{"name": "search_inventory", "arguments": {"price_max": 30000}}]
    assert not ungrounded(check("Here are cars under $30,000.", [car(1, 21834)], calls=calls))


def test_a_result_count_must_match_the_tool():
    rows = [car(i, 20000 + i) for i in range(5)]
    assert not ungrounded(check("I found 34 matching cars.", rows, counts=34))
    assert [v["value"] for v in ungrounded(check("I found 40 matching cars.", rows, counts=34))] == [40]
    assert not ungrounded(check("Here are five options.", rows, counts=34))


def test_mileage_must_belong_to_a_returned_listing():
    rows = [car(1, 21834, mileage="41000")]
    assert not ungrounded(check("It has 41,000 miles.", rows))
    assert [v["type"] for v in ungrounded(check("It has 12,000 miles.", rows))] == ["mileage"]


def test_bedrooms_baths_and_size_must_belong_to_a_listing():
    rows = [home(1, 350000, bed="3", bath="2", size="1500")]
    assert not ungrounded(check("A 3 bed, 2 bath home of 1,500 sq ft for $350,000.", rows))
    bad = ungrounded(check("A 4 bed, 3 bath home of 2,400 sq ft for $350,000.", rows))
    assert {v["type"] for v in bad} == {"bedrooms", "bathrooms", "size"}


def test_a_compact_listing_label_is_read_as_bedrooms_and_baths():
    rows = [home(1, 350000, bed="3", bath="2")]
    assert not ungrounded(check("3bd/2ba in Salem, $350,000", rows))
    assert {v["type"] for v in ungrounded(check("5bd/1ba in Salem, $350,000", rows))} == {"bedrooms", "bathrooms"}


def test_a_model_year_must_be_in_the_results_or_the_customers_words():
    rows = [car(1, 21834, year="2020")]
    assert not ungrounded(check("A 2020 Camry for $21,834.", rows))
    assert [v["type"] for v in ungrounded(check("A 2018 Camry for $21,834.", rows))] == ["year"]
    assert not ungrounded(check("Nothing from 2023 or newer.", [], message="2023 or newer please", counts=0))


def test_a_city_that_is_not_in_the_results_is_ungrounded():
    rows = [car(1, 21834, location="Austin, TX")]
    assert not ungrounded(check("It is in Austin, TX, for $21,834.", rows))
    assert [v["type"] for v in ungrounded(check("It is in Dallas, TX, for $21,834.", rows))] == ["location"]


def test_claiming_nothing_was_found_when_results_exist_is_ungrounded():
    rows = [car(1, 21834)]
    assert [v["type"] for v in ungrounded(check("Sorry, I couldn't find any matching cars.", rows))] == ["none_found"]
    assert not ungrounded(check("Sorry, I couldn't find any matching cars.", [], counts=0))


def test_none_of_these_is_fine_when_the_reply_names_what_it_did_find():
    rows = [home(1, 350000, city="Salem")]
    assert not ungrounded(check("None of these are townhouses, but I found a 3bd/2ba in Salem for $350,000.", rows))


def test_history_figures_may_be_restated():
    context = "range rover in cincinnati? I found 2 Range Rovers: $34,726, 73,876 miles and $40,470, 54,442 miles."
    assert not ungrounded(check("Holding the $40,470 one with 54,442 miles.", [], message=context, counts=0))


def test_the_evidence_catalog_of_a_no_tools_baseline_counts_as_the_results():
    verdicts = claims.check_reply("The Camry is $21,834.", tool_results=[], tool_calls=[], context_text="", evidence_rows=[car(1, 21834)])
    assert not ungrounded(verdicts)


def test_a_reply_without_claims_has_no_verdicts():
    assert check("What is your budget?", [car(1, 21834)]) == []


def test_sample_for_audit_is_random_but_repeatable_and_sheet_lists_claims(tmp_path):
    records = [
        {"case_id": f"c{i}", "repeat": 0, "actual": {"reply": f"Car {i} costs ${i}00.", "tool_results": [], "tool_calls_made": []},
         "message": "hi", "grounding": {"claims": [{"type": "price", "text": f"${i}00", "grounded": i % 2 == 0, "why": ""}]}}
        for i in range(1, 41)
    ]
    first = claims.sample_for_audit(records, 30, seed=7)
    second = claims.sample_for_audit(records, 30, seed=7)
    assert len(first) == 30 and [r["case_id"] for r in first] == [r["case_id"] for r in second]
    assert [r["case_id"] for r in first] != [r["case_id"] for r in claims.sample_for_audit(records, 30, seed=8)]
    sheet = claims.audit_sheet(first)
    assert sheet.splitlines()[0].startswith("case_id,repeat,reply,claims,verdict")
    assert len(sheet.splitlines()) >= 31


def test_sample_for_audit_takes_all_when_there_are_fewer_records():
    records = [{"case_id": "a", "repeat": 0, "actual": {"reply": "x"}, "grounding": {"claims": []}}]
    assert len(claims.sample_for_audit(records, 30, seed=1)) == 1


@pytest.mark.parametrize("word,number", [("one", 1), ("three", 3), ("ten", 10)])
def test_number_words_are_read_in_counts(word, number):
    rows = [car(i, 20000 + i) for i in range(number)]
    assert not ungrounded(check(f"Here are {word} options.", rows, counts=number))


def test_a_date_in_the_reply_is_not_a_model_year():
    rows = [car(1, 20000)]
    for reply in (
        "This Saturday would be November 14, 2026. Which time of day suits you?",
        "I can do Saturday, Nov 14 2026 in the morning.",
        "How about 14 November 2026?",
        "Booked for 2026-11-14 if that works.",
    ):
        assert not [v for v in check(reply, rows) if v["type"] == "year"], reply


def test_a_real_model_year_is_still_checked_next_to_a_date():
    rows = [car(1, 20000, year="2020")]
    verdicts = check("The 2026 Toyota Camry is available, and I can see you on November 14, 2026.", rows)
    years = [v for v in verdicts if v["type"] == "year"]
    assert [v["value"] for v in years] == [2026] and not years[0]["grounded"]


def test_square_feet_written_with_a_dot_as_the_thousands_separator_are_read_in_full():
    rows = [home(1, 99000, size="1320")]
    verdicts = [v for v in check("3 Schlafzimmer, 99.000 $ (1.320 sq ft)", rows) if v["type"] == "size"]
    assert [v["value"] for v in verdicts] == [1320.0] and verdicts[0]["grounded"]
    verdicts = [v for v in check("A home of 1,320 sq ft.", rows) if v["type"] == "size"]
    assert [v["value"] for v in verdicts] == [1320.0] and verdicts[0]["grounded"]
    verdicts = [v for v in check("A home of 950 sq ft.", rows) if v["type"] == "size"]
    assert verdicts and not verdicts[0]["grounded"]


def test_over_n_is_true_when_the_real_count_is_higher():
    rows = [car(i, 10000 + i) for i in range(5)]
    assert not ungrounded(check("We have over 300 cars in the catalog.", rows, counts=338))
    assert not ungrounded(check("There are more than 300 cars.", rows, counts=338))
    assert ungrounded(check("We have over 400 cars in the catalog.", rows, counts=338))
    assert ungrounded(check("We have 300 cars in the catalog.", rows, counts=338))
