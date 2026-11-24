import json

import pytest

from eval import trace_summary


def trace(latency, cost, *, prompt=100, completion=50, failover=False, retry=False, tools=()):
    return {
        "latency_ms": latency,
        "cost": cost,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "failover_used": failover,
        "retry_round_used": retry,
        "tools": [{"name": name, "outcome": outcome, "duration_ms": 5} for name, outcome in tools],
    }


@pytest.fixture
def fixture_file(tmp_path):
    records = [
        trace(100, 0.01),
        trace(200, 0.02, failover=True),
        trace(300, 0.03, retry=True, tools=[("search_inventory", "ok")]),
        trace(1000, 0.04, tools=[("create_lead", "ok")]),
        trace(400, 0.10, tools=[("create_lead", "validation_error")]),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n\n[1, 2]\n", encoding="utf-8")
    return path


def test_the_summary_figures(fixture_file):
    traces, skipped = trace_summary.load_traces(fixture_file)
    summary = trace_summary.summarize(traces)
    assert skipped == 2 and summary["turns"] == 5
    assert summary["mean_latency_ms"] == pytest.approx(400)
    assert summary["p95_latency_ms"] == 1000
    assert summary["mean_tokens_per_turn"] == pytest.approx(150)
    assert summary["total_cost"] == pytest.approx(0.20)
    assert summary["mean_cost_per_turn"] == pytest.approx(0.04)
    assert summary["failover_share"] == pytest.approx(0.2) and summary["retry_share"] == pytest.approx(0.2)


def test_cost_per_lead_counts_only_turns_where_create_lead_succeeded(fixture_file):
    summary = trace_summary.summarize(trace_summary.load_traces(fixture_file)[0])
    assert summary["lead_turns"] == 1 and summary["cost_per_lead"] == pytest.approx(0.04)


def test_with_no_lead_the_report_says_so():
    summary = trace_summary.summarize([trace(100, 0.01), trace(200, 0.02)])
    assert summary["cost_per_lead"] is None
    assert "no turn created a lead" in trace_summary.format_summary(summary)


def test_main_prints_the_report(fixture_file, capsys):
    assert trace_summary.main([str(fixture_file)]) == 0
    out = capsys.readouterr().out
    for label in ("turns:", "latency mean / p95:", "mean tokens per turn:", "cost per lead:", "skipped lines:"):
        assert label in out


def test_an_empty_file_and_a_missing_file(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert trace_summary.main([str(empty)]) == 0
    assert "No turns" in capsys.readouterr().out
    assert trace_summary.main([str(tmp_path / "missing.jsonl")]) == 1


# How the conversations went


def turn(chat, *, source="text", asked=False, shown=0, tools=()):
    return {
        **trace(100, 0.01, tools=tools), "chat_ref": chat, "source": source, "asked_for_person": asked, "buttons_shown": shown,
    }


def talk(records):
    result = trace_summary.conversation_metrics(records)
    assert result is not None
    return result


def test_chats_that_stop_after_one_reply_and_chats_that_reach_a_request_are_counted_by_chat():
    records = [
        turn("a"),  # one turn, left
        turn("b"), turn("b", tools=[("reserve_item", "ok")]),  # two turns, a hold
        turn("c"), turn("c"), turn("c", tools=[("book_viewing", "ok")]),  # three turns, a viewing
        turn("d"), turn("d", tools=[("create_lead", "validation_error")]),  # a lead that failed is not a request
    ]
    metrics = talk(records)
    assert metrics["chats"] == 4
    assert metrics["stopped_after_first_reply"] == pytest.approx(0.25)
    assert metrics["chats_with_request"] == pytest.approx(0.5)
    assert metrics["turns_per_request_chat"] == pytest.approx(2.5)  # chats b (2 turns) and c (3 turns)


def test_asking_for_a_person_is_counted_once_per_chat():
    records = [turn("a", asked=True), turn("a", asked=True), turn("b"), turn("c", asked=True), turn("d")]
    assert talk(records)["asked_for_person"] == pytest.approx(0.5)


def test_tap_through_is_the_share_of_replies_with_buttons_that_a_tap_followed_in_that_chat():
    records = [
        turn("a", shown=3), turn("a", source="button"),  # buttons, then a tap
        turn("b", shown=2), turn("b"),  # buttons, then typed text: no tap
        turn("c", source="button"), turn("c", shown=4),  # a tap came before the buttons: does not count
    ]
    metrics = talk(records)
    assert metrics["replies_with_buttons"] == 3
    assert metrics["tap_through"] == pytest.approx(1 / 3)
    assert metrics["button_turns"] == pytest.approx(2 / 6)


def test_with_no_buttons_the_tap_through_says_so_and_without_chat_refs_there_are_no_conversation_figures():
    assert talk([turn("a"), turn("b")])["tap_through"] is None
    assert trace_summary.conversation_metrics([trace(100, 0.01)]) is None  # an old trace has no chat_ref
    assert trace_summary.summarize([trace(100, 0.01)])["conversation"] is None


def test_the_report_prints_the_conversation_lines_only_when_there_are_any():
    with_chats = trace_summary.format_summary(trace_summary.summarize([turn("a", shown=2), turn("a", source="button"), turn("b")]))
    for label in ("chats:", "stopped after 1 reply:", "chats with a request:", "asked for a person:", "button taps:"):
        assert label in with_chats
    assert "tap-through 100%" in with_chats
    without = trace_summary.format_summary(trace_summary.summarize([trace(100, 0.01)]))
    assert "stopped after 1 reply" not in without
