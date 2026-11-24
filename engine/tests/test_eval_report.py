import importlib.util
import json

import pytest

from eval import report, results_io

needs_matplotlib = pytest.mark.skipif(
    importlib.util.find_spec("matplotlib") is None, reason="matplotlib is in requirements-tools.txt, not the engine's"
)


def header(kind="agent", domain="cars", dataset="heldout_cars.json", repeats=2, **extra):
    return results_io.build_header(
        kind=kind, domain=domain, dataset_name=dataset, dataset_sha256="ab" * 32, repeats=repeats, reference_date="2026-11-04",
        sampling={"temperature": 0.0, "max_tokens": 800, "seed": None}, prompt_version="pv1234567890",
        models_configured=["vendor/model-a:free"], argv=["python", "-m", "eval.run_eval"], run_date="2026-11-04",
        git_sha="abc123", git_dirty=False, **extra,
    )


def agent_record(case_id, repeat, passed, category="cat", model="vendor/model-a:free"):
    reply = "A car for $100." if passed else "A car for $999."
    return {
        "case_id": case_id, "repeat": repeat, "category": category, "set": "heldout", "message": "m", "history": None,
        "expected": {"tool": "search_inventory", "accept_tools": [], "args": {}, "outcome": "o", "rules": [{"rule": "reply_nonempty"}], "resolution_errors": []},
        "actual": {"reply": reply, "tool_calls_made": [{"name": "search_inventory", "arguments": {}}],
                   "tool_results": [{"matches": [{"id": 1, "name": "x", "price": 100.0, "attributes": "{}"}], "count": 1}]},
        "latency_ms": 1000, "models": [model, model], "tokens": {"prompt": 100, "completion": 10}, "cost": 0.0,
    }


def write_run(path, hdr, records, complete=True):
    results_io.start_run(path, hdr)
    for r in records:
        results_io.append_record(path, r)
    results_io.write_footer(path, {"complete": complete, "stopped": None if complete else "BudgetExhausted", "expected_records": len(records), "records_without_error": len(records)})


@pytest.fixture
def results_root(tmp_path):
    day = tmp_path / "2026-11-04-heldout"
    day.mkdir()
    records = [agent_record("a", 0, True), agent_record("a", 1, True), agent_record("b", 0, True), agent_record("b", 1, False)]
    write_run(day / "cars_heldout.jsonl", header(), records)
    return tmp_path


def test_a_file_without_provenance_stops_the_report(results_root):
    bad = results_root / "2026-11-04-heldout" / "old.jsonl"
    bad.write_text(json.dumps(agent_record("a", 0, True)) + "\n", encoding="utf-8")
    with pytest.raises(results_io.ProvenanceError):
        report.load_runs(results_root)


def test_legacy_json_files_are_ignored_not_read(results_root):
    (results_root / "cars_results.json").write_text("[]", encoding="utf-8")
    assert len(report.load_runs(results_root)) == 1


def test_the_headline_table_has_intervals_n_model_and_file(results_root):
    runs = report.load_runs(results_root)
    table = report.render_agent_table(runs, "heldout")
    assert "cars" in table and "vendor/model-a:free" in table and "cars_heldout.jsonl" in table
    assert "pass@1" in table and "pass^2" in table
    assert "75.0% [" in table  # 3 of 4 trials pass
    assert "2 cases" in table or "| 2 |" in table


def test_an_incomplete_run_is_labelled_not_presented_as_complete(tmp_path):
    day = tmp_path / "2026-11-04-heldout"
    day.mkdir()
    write_run(day / "cars_heldout.jsonl", header(), [agent_record("a", 0, True)], complete=False)
    table = report.render_agent_table(report.load_runs(tmp_path), "heldout")
    assert "INCOMPLETE" in table


def test_a_run_that_changed_model_is_flagged_mixed(tmp_path):
    day = tmp_path / "2026-11-04-heldout"
    day.mkdir()
    records = [agent_record("a", 0, True, model="m1"), agent_record("b", 0, True, model="m2")]
    write_run(day / "cars_heldout.jsonl", header(repeats=1), records)
    runs = report.load_runs(tmp_path)
    assert "MIXED" in report.render_agent_table(runs, "heldout")
    assert "m1" in report.render_provenance_table(runs) and "m2" in report.render_provenance_table(runs)


def test_the_baseline_table_puts_agent_and_baselines_side_by_side(results_root):
    day = results_root / "2026-11-04-heldout"
    write_run(day / "cars_heldout_router.jsonl", header(kind="keyword_router"), [agent_record("a", 0, False, model="keyword-router"), agent_record("b", 0, False, model="keyword-router"),
                                                                            agent_record("a", 1, False, model="keyword-router"), agent_record("b", 1, False, model="keyword-router")])
    table = report.render_baseline_table(report.load_runs(results_root))
    assert "agent" in table and "keyword router" in table and "0.0% [" in table


def redteam_record(case_id, repeat, arm, failures):
    return {"case_id": case_id, "parent_id": None, "variant": None, "repeat": repeat, "arm": arm, "domain": "cars", "category": "x",
            "failures": failures, "models": ["m1"], "tokens": {"prompt": 1, "completion": 1}, "cost": 0.0}


def test_the_redteam_table_compares_on_with_each_guardrail_off_on_the_same_attacks(tmp_path):
    day = tmp_path / "2026-11-04-redteam"
    day.mkdir()
    on = [redteam_record(i, r, "on", []) for i in (1, 2, 3, 4) for r in (0, 1)]
    off = [redteam_record(i, r, "off:tool_call_cap", ["3 leads created"] if r == 0 else []) for i in (1, 2) for r in (0, 1)]
    write_run(day / "redteam_cars_originals_on.jsonl", header(kind="redteam", dataset="redteam_set.json", arm="on", guardrails_off=[]), on)
    write_run(day / "redteam_cars_originals_off-tool_call_cap.jsonl", header(kind="redteam", dataset="redteam_set.json", arm="off:tool_call_cap", guardrails_off=["tool_call_cap"]), off)
    table = report.render_redteam_table(report.load_runs(tmp_path))
    assert "tool_call_cap" in table
    assert "0.0% [" in table and "50.0% [" in table  # on: 0/4 on those two attacks... off: 2/4
    assert "inconclusive" in table


def test_the_benign_table_compares_guardrails_on_and_off(tmp_path):
    day = tmp_path / "2026-11-04-dev"
    day.mkdir()
    recs = [agent_record("a", 0, True), agent_record("b", 0, False)]
    write_run(day / "cars_dev.jsonl", header(dataset="cars_test_set.json", repeats=1, guardrails_off=[]), recs)
    write_run(day / "cars_dev_guardrails-off.jsonl", header(dataset="cars_test_set.json", repeats=1, guardrails_off=["injection_screen"]), recs[:1])
    table = report.render_benign_table(report.load_runs(tmp_path))
    assert "on" in table and "off" in table and "50.0% [" in table and "100.0% [" in table


def test_markers_are_replaced_and_missing_markers_are_an_error():
    text = "intro\n<!-- BEGIN:t1 -->\nold\n<!-- END:t1 -->\noutro\n"
    assert report.update_markers(text, {"t1": "new table"}) == "intro\n<!-- BEGIN:t1 -->\nnew table\n<!-- END:t1 -->\noutro\n"
    with pytest.raises(KeyError, match="t2"):
        report.update_markers(text, {"t2": "x"})


@needs_matplotlib
def test_the_chart_is_written_from_the_summaries(results_root, tmp_path):
    out = tmp_path / "charts"
    paths = report.write_charts(report.load_runs(results_root), out)
    assert paths and all(p.exists() and p.stat().st_size > 1000 for p in paths)


@needs_matplotlib
def test_the_command_rewrites_report_md_from_stored_files_only(results_root, tmp_path):
    md = tmp_path / "REPORT.md"
    markers = "".join(f"<!-- BEGIN:{n} -->\nx\n<!-- END:{n} -->\n" for n in report.TABLE_NAMES)
    md.write_text("# R\n" + markers, encoding="utf-8")
    code = report.main(["--results-root", str(results_root), "--report", str(md), "--charts-dir", str(tmp_path / "ch")])
    assert code == 0
    text = md.read_text(encoding="utf-8")
    assert "vendor/model-a:free" in text and "\nx\n" not in text


def test_the_command_refuses_files_without_provenance(results_root, tmp_path):
    (results_root / "2026-11-04-heldout" / "bad.jsonl").write_text('{"case_id": 1}\n', encoding="utf-8")
    md = tmp_path / "REPORT.md"
    md.write_text("# R\n", encoding="utf-8")
    assert report.main(["--results-root", str(results_root), "--report", str(md), "--charts-dir", str(tmp_path / "ch")]) == 1
    assert md.read_text(encoding="utf-8") == "# R\n"


def test_the_audit_sheet_command_samples_a_results_file(results_root, tmp_path):
    from eval import audit_sheet

    out = tmp_path / "sheet.csv"
    path = results_root / "2026-11-04-heldout" / "cars_heldout.jsonl"
    assert audit_sheet.main([str(path), "--n", "3", "--seed", "2", "--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("case_id,repeat,reply,claims,verdict") and len(lines) == 4
    assert any("ungrounded" in line for line in lines) or any("all grounded" in line for line in lines)


def test_the_audit_sheet_command_refuses_a_file_without_a_header(tmp_path):
    from eval import audit_sheet

    bad = tmp_path / "x.jsonl"
    bad.write_text('{"case_id": 1}\n', encoding="utf-8")
    assert audit_sheet.main([str(bad)]) == 1


def test_all_off_is_compared_with_guardrails_on_runs_of_the_same_attacks_from_another_set(tmp_path):
    day = tmp_path / "2026-11-04-redteam"
    day.mkdir()
    on = [redteam_record(i, 0, "on", []) for i in (1, 2)]
    off = [redteam_record(i, 0, "all_off", ["leaked"]) for i in (1, 2)]
    write_run(day / "a_on.jsonl", header(kind="redteam", dataset="redteam_set.json", arm="on", guardrails_off=[]), on)
    write_run(day / "b_alloff.jsonl", header(kind="redteam", dataset="other_set.json", arm="all_off", guardrails_off=["x"]), off)
    table = report.render_redteam_table(report.load_runs(tmp_path))
    assert "no guardrails-on run" not in table and "100.0% [" in table and "0.0% [" in table


@needs_matplotlib
def test_the_chart_survives_a_zero_rate_whose_interval_touches_the_rate(tmp_path):
    day = tmp_path / "2026-11-04-redteam"
    day.mkdir()
    write_run(day / "a_on.jsonl", header(kind="redteam", dataset="redteam_set.json", arm="on", guardrails_off=[]),
              [redteam_record(i, 0, "on", []) for i in range(5)])
    assert report.write_charts(report.load_runs(tmp_path), tmp_path / "ch")


def test_a_file_in_a_folder_marked_partial_is_flagged_superseded(tmp_path):
    day = tmp_path / "2026-11-04-other-model-partial"
    day.mkdir()
    write_run(day / "cars_heldout.jsonl", header(), [agent_record("a", 0, True)])
    assert "SUPERSEDED" in report.render_agent_table(report.load_runs(tmp_path), "heldout")


@pytest.fixture
def mixed_root(tmp_path):
    for folder, dataset in (("2026-11-11-fresh2-old", "fresh2_cars.json"), ("2026-11-11-fresh2-new", "fresh2_cars.json"), ("2026-11-04-dev", "cars_test_set.json")):
        day = tmp_path / folder
        day.mkdir()
        write_run(day / "cars_x.jsonl", header(dataset=dataset), [agent_record("a", 0, True), agent_record("a", 1, True)])
    return tmp_path


def test_fresh_dev_and_heldout_runs_are_told_apart(mixed_root):
    kinds = {r.path.parent.name: r.dataset_kind for r in report.load_runs(mixed_root)}
    assert kinds == {"2026-11-11-fresh2-old": "fresh", "2026-11-11-fresh2-new": "fresh", "2026-11-04-dev": "dev"}


def test_the_fresh_table_lists_each_run_by_folder_and_file_and_leaves_out_dev(mixed_root):
    table = report.render_agent_table(report.load_runs(mixed_root), "fresh")
    assert "2026-11-11-fresh2-old/cars_x.jsonl" in table and "2026-11-11-fresh2-new/cars_x.jsonl" in table
    assert "2026-11-04-dev" not in table
    assert "2026-11-11-fresh2-new" not in report.render_agent_table(report.load_runs(mixed_root), "dev")


def test_the_fresh_baseline_table_names_the_file(mixed_root):
    table = report.render_baseline_table(report.load_runs(mixed_root), "fresh")
    assert "2026-11-11-fresh2-old/cars_x.jsonl" in table and "file" in table.splitlines()[0]


def test_per_run_ledgers_are_added_to_the_call_count(tmp_path):
    (tmp_path / "call_ledger.json").write_text(json.dumps({"calls": 100, "by_label": {}}), encoding="utf-8")
    folder = tmp_path / "ledgers-2026-11-11"
    folder.mkdir()
    (folder / "a.json").write_text(json.dumps({"calls": 40}), encoding="utf-8")
    (folder / "b.json").write_text(json.dumps({"calls": 2}), encoding="utf-8")
    text = report.render_calls(tmp_path)
    assert "**100**" in text and "2 ledgers" in text and "**42**" in text


def test_a_truncated_run_is_flagged_as_superseded(tmp_path):
    day = tmp_path / "2026-11-11-oldcode-large4-effort-high-truncated"
    day.mkdir()
    write_run(day / "cars_heldout.jsonl", header(), [agent_record("a", 0, True)])
    assert "SUPERSEDED" in report._flags(report.load_runs(tmp_path)[0])
