import json
from typing import Any

import pytest

from eval import metrics

SEARCH = "search_inventory"


def rec(case_id="c1", repeat=0, *, tool: "str | None" = SEARCH, accept: Any = (), args: Any = None, rules: Any = (), reply: Any = "Here you go.", calls: Any = (), results: Any = (),
        message: Any = "m", category: Any = "cat", error: Any = None, errors: Any = (), history: Any = None, latency: Any = 1000):
    actual = {"reply": reply, "tool_calls_made": list(calls), "tool_results": list(results)}
    if error:
        actual = {"error": error, "reply": None, "tool_calls_made": [], "tool_results": []}
    return {
        "case_id": case_id, "repeat": repeat, "category": category, "message": message, "history": history,
        "expected": {"tool": tool, "accept_tools": list(accept), "args": args or {}, "outcome": "o",
                     "rules": [{"rule": "reply_nonempty"}, *rules], "resolution_errors": list(errors)},
        "actual": actual, "latency_ms": latency,
    }


def call(tool, /, **arguments):
    return {"name": tool, "arguments": arguments}


def car(price):
    return {"id": 1, "name": "2020 Toyota Camry", "price": float(price), "attributes": json.dumps({"year": "2020", "mileage": "41000", "location": "Austin, TX"})}


def result(*prices):
    return {"matches": [car(p) for p in prices], "count": len(prices)}


def grade(**kw):
    return metrics.grade_record(rec(**kw))


def test_right_tool_right_args_good_reply_passes_every_check():
    g = grade(args={"make": "Toyota", "price_max": 30000}, calls=[call(SEARCH, make="toyota", price_max="30,000")],
              results=[result(21834)], reply="The 2020 Camry is $21,834.")
    assert (g["tool_ok"], g["slot_ok"], g["outcome_ok"], g["grounding_ok"], g["case_pass"]) == (True, True, True, True, True)


def test_the_wrong_tool_fails_tool_selection_and_the_case():
    g = grade(tool="create_lead", calls=[call(SEARCH)], results=[result(100)])
    assert g["tool_ok"] is False and g["case_pass"] is False


def test_a_wrong_argument_fails_slots_but_not_the_tool():
    g = grade(args={"make": "Toyota"}, calls=[call(SEARCH, make="Honda")], results=[result(100)])
    assert g["tool_ok"] is True and g["slot_ok"] is False and g["case_pass"] is False


def test_no_graded_arguments_means_slots_are_not_applicable():
    g = grade(args={}, calls=[call(SEARCH)], results=[result(100)])
    assert g["slot_ok"] is None and g["case_pass"] is True


def test_the_lead_name_is_not_graded_but_the_other_fields_are():
    args = {"name": "Toyota RAV4", "customer_name": "John", "customer_contact": "j@example.com", "price": 100}
    ok = grade(tool="create_lead", args=args, calls=[call("create_lead", name="anything", customer_name="John", customer_contact="j@example.com", price=100)])
    assert ok["slot_ok"] is True
    bad = grade(tool="create_lead", args=args, calls=[call("create_lead", name="x", customer_name="John", customer_contact="j@example.org", price=100)])
    assert bad["slot_ok"] is False


def test_an_expected_none_matches_an_absent_argument():
    g = grade(args={"price_max": None}, calls=[call(SEARCH)], results=[result(100)])
    assert g["slot_ok"] is True


def test_matchers_work_inside_expected_args():
    g = grade(args={"price_max": {"$between": [25000, 30000]}}, calls=[call(SEARCH, price_max=27500)], results=[result(100)])
    assert g["slot_ok"] is True
    g = grade(args={"price_max": {"$between": [25000, 30000]}}, calls=[call(SEARCH, price_max=45000)], results=[result(100)])
    assert g["slot_ok"] is False


def test_an_accepted_alternative_tool_counts_as_the_right_choice():
    assert grade(tool=SEARCH, accept=[None], calls=[], reply="What is your budget?")["tool_ok"] is True
    assert grade(tool=SEARCH, accept=[None], calls=[call("create_lead")])["tool_ok"] is False


def test_no_tool_expected_and_one_called_fails():
    g = grade(tool=None, calls=[call(SEARCH)], results=[result(100)], rules=[{"rule": "no_tool_called"}])
    assert g["tool_ok"] is False and g["outcome_ok"] is False


def test_a_refusal_is_graded_on_its_outcome_rules_not_just_on_no_tool():
    refusal = {"rule": "reply_no_link"}
    good = grade(tool=None, reply="I can only help with cars.", rules=[refusal])
    bad = grade(tool=None, reply="Try http://evil.example", rules=[refusal])
    assert good["case_pass"] is True and bad["case_pass"] is False
    assert bad["rule_results"][-1] == {"rule": "reply_no_link", "ok": False, "why": bad["rule_results"][-1]["why"]}


def test_the_did_not_catch_that_fallback_is_a_failed_outcome():
    g = grade(tool=SEARCH, calls=[call(SEARCH)], results=[result(100)], reply="Sorry, I didn't catch that. Could you tell me what you're looking for?")
    assert g["tool_ok"] is True and g["outcome_ok"] is False and g["case_pass"] is False


def test_an_invented_price_fails_grounding_and_the_case():
    g = grade(calls=[call(SEARCH)], results=[result(21834)], reply="The Camry is $19,999.")
    assert g["grounding_ok"] is False and g["case_pass"] is False
    assert g["claims"][0]["grounded"] is False


def test_the_old_price_only_check_is_kept_as_its_own_number():
    flagged = grade(calls=[call(SEARCH)], results=[result(21834)], reply="The Camry is $19,999.")
    clean = grade(calls=[call(SEARCH)], results=[result(21834)], reply="The Camry is $21,834.")
    assert flagged["price_only_flag"] is True and clean["price_only_flag"] is False
    assert grade(tool=None, reply="hello")["price_only_flag"] is None


def test_an_errored_case_fails_everything_and_is_counted_as_an_error():
    g = grade(error="HTTPStatusError: 429")
    assert g["error"] is True and g["case_pass"] is False and g["tool_ok"] is False


def test_an_unresolvable_label_is_excluded_not_failed():
    g = grade(errors=["item_id: no catalog item fits"], calls=[call(SEARCH)], results=[result(1)])
    assert g["excluded"] is True and g["case_pass"] is None


def test_history_prices_may_be_restated_in_the_reply():
    history = [{"role": "assistant", "content": "I found a car for $40,470."}]
    g = grade(history=history, calls=[call(SEARCH)], results=[result(100)], reply="That $40,470 car is great, and one is $100.")
    assert g["grounding_ok"] is True


def test_a_no_tools_baseline_is_grounded_against_the_catalog_it_was_shown():
    r = rec(tool=None, reply="The Camry is $21,834.", calls=[], results=[])
    r["actual"]["evidence_rows"] = [car(21834)]
    assert metrics.grade_record(r)["grounding_ok"] is True


def summarise(records, k):
    return metrics.summarize_records([metrics.grade_record(r) for r in records], k)


def test_summary_reports_each_metric_separately_with_intervals():
    records = [
        rec("a", 0, calls=[call(SEARCH)], results=[result(100)], reply="A car for $100."),
        rec("b", 0, tool="create_lead", calls=[call(SEARCH)], results=[result(100)], reply="A car for $100."),
        rec("c", 0, calls=[call(SEARCH)], results=[result(100)], reply="A car for $999."),
        rec("d", 0, error="boom"),
    ]
    s = summarise(records, 1)
    assert s["n_records"] == 4 and s["n_errors"] == 1
    assert (s["tool_selection"]["successes"], s["tool_selection"]["n"]) == (2, 4)
    assert (s["grounding"]["successes"], s["grounding"]["n"]) == (2, 3)
    assert (s["case_pass"]["successes"], s["case_pass"]["n"]) == (1, 4)
    for key in ("tool_selection", "slot_extraction", "outcome", "grounding", "case_pass"):
        assert {"p", "low", "high"} <= set(s[key])
    assert 0 <= s["case_pass"]["low"] <= s["case_pass"]["p"] <= s["case_pass"]["high"] <= 1


def test_summary_pass_k_needs_a_case_to_pass_every_repeat():
    good: dict = dict(calls=[call(SEARCH)], results=[result(100)], reply="A car for $100.")
    bad: dict = dict(calls=[call(SEARCH)], results=[result(100)], reply="A car for $999.")
    records = [rec("a", 0, **good), rec("a", 1, **good), rec("b", 0, **good), rec("b", 1, **bad)]
    s = summarise(records, 2)
    assert s["pass_k"]["pass_at_1"] == pytest.approx(0.75)
    assert s["pass_k"]["pass_hat_k"] == pytest.approx(0.5)
    assert s["pass_k"]["n_complete_cases"] == 2


def test_summary_by_category_splits_the_counts():
    good: dict = dict(calls=[call(SEARCH)], results=[result(100)], reply="A car for $100.")
    s = summarise([rec("a", 0, category="x", **good), rec("b", 0, category="y", **good)], 1)
    assert set(s["by_category"]) == {"x", "y"}
    assert s["by_category"]["x"]["case_pass"]["n"] == 1


def test_an_empty_run_summarises_without_dividing_by_zero():
    s = metrics.summarize_records([], 1)
    assert s["n_records"] == 0 and s["case_pass"]["n"] == 0


def test_latency_has_a_mean_and_an_interval():
    s = summarise([rec("a", 0, latency=1000, tool=None, reply="hello there"), rec("b", 0, latency=3000, tool=None, reply="hello there")], 1)
    assert s["latency_ms"]["mean"] == 2000


def test_a_search_followed_by_the_expected_booking_counts_as_choosing_the_booking_tool():
    g = grade(tool="book_viewing", args={"item_id": 1}, calls=[call(SEARCH, make="Toyota"), call("book_viewing", item_id=1)])
    assert g["tool_ok"] is True


def test_searching_then_acting_when_no_action_was_expected_still_fails():
    assert grade(tool=None, calls=[call(SEARCH, make="Toyota"), call("create_lead", name="x")])["tool_ok"] is False
    assert grade(tool=None, calls=[call(SEARCH, make="Toyota")])["tool_ok"] is False


def test_a_search_alone_is_still_the_search_tool():
    assert grade(calls=[call(SEARCH, make="Toyota")])["tool_ok"] is True
    assert grade(tool="book_viewing", calls=[call(SEARCH, make="Toyota")])["tool_ok"] is False


def test_the_wrong_action_after_a_search_fails():
    assert grade(tool="book_viewing", calls=[call(SEARCH), call("create_lead", name="x")])["tool_ok"] is False


def test_a_day_name_written_in_a_rule_gets_the_dated_matcher_from_the_expected_arguments():
    matcher = {"$weekday_after": {"weekday": 1, "after": "2026-11-11"}}
    rule = {"rule": "arg_matches", "tool": "book_viewing", "arg": "date", "expect": "$weekday:tuesday"}
    good = metrics.grade_record(rec(tool="book_viewing", args={"date": matcher}, rules=[rule], calls=[call("book_viewing", date="2026-11-17")]))
    wrong_day = metrics.grade_record(rec(tool="book_viewing", args={"date": matcher}, rules=[rule], calls=[call("book_viewing", date="2026-11-18")]))
    assert good["outcome_ok"] is True
    assert wrong_day["outcome_ok"] is False
