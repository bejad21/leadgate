import json

import pytest

from eval import results_io


def header(**overrides):
    base: dict = dict(
        kind="agent", domain="cars", dataset_name="heldout_cars.json", dataset_sha256="ab" * 32, repeats=5,
        reference_date="2026-11-04", sampling={"temperature": 0.0, "max_tokens": 800, "seed": None},
        prompt_version="2b2c5b81b382", models_configured=["nvidia/x:free"], argv=["python", "-m", "eval.run_eval"],
        run_date="2026-11-04", git_sha="abc1234", git_dirty=False,
    )
    base.update(overrides)
    return results_io.build_header(**base)  # type: ignore[arg-type]


def record(case_id="a", repeat=0, **extra):
    return {"case_id": case_id, "repeat": repeat, "actual": {"reply": "hi"}, "models": ["m1"],
            "tokens": {"prompt": 10, "completion": 5}, "cost": 0.0, **extra}


def test_the_header_names_everything_a_reader_needs():
    h = header()
    for key in ("model_configured", "run_date", "git_sha", "git_dirty", "prompt_version", "dataset", "sampling",
                "reference_date", "repeats", "command", "kind", "domain"):
        assert key in h, key
    assert h["dataset"] == {"name": "heldout_cars.json", "sha256": "ab" * 32}
    assert h["command"] == "python -m eval.run_eval"
    assert h["sampling"]["temperature"] == 0.0


def test_a_written_run_reads_back_with_its_header(tmp_path):
    path = tmp_path / "cars_heldout.jsonl"
    results_io.start_run(path, header())
    results_io.append_record(path, record("a", 0))
    results_io.append_record(path, record("b", 0))
    run = results_io.read_run(path)
    assert run.header["domain"] == "cars"
    assert [r["case_id"] for r in run.records] == ["a", "b"]


def test_a_file_without_a_header_is_rejected(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    with pytest.raises(results_io.ProvenanceError, match="header"):
        results_io.read_run(path)


def test_an_old_style_json_list_is_rejected(tmp_path):
    path = tmp_path / "cars_results.json"
    path.write_text(json.dumps([{"message": "m", "expected": {}, "actual": {}}]), encoding="utf-8")
    with pytest.raises(results_io.ProvenanceError):
        results_io.read_run(path)


@pytest.mark.parametrize("missing", ["model_configured", "git_sha", "prompt_version", "dataset", "sampling", "reference_date", "command"])
def test_a_header_missing_a_required_field_is_rejected(tmp_path, missing):
    path = tmp_path / "r.jsonl"
    h = header()
    del h[missing]
    path.write_text(json.dumps({"_header": h}) + "\n" + json.dumps(record()) + "\n", encoding="utf-8")
    with pytest.raises(results_io.ProvenanceError, match=missing):
        results_io.read_run(path)


def test_the_last_record_for_a_case_and_repeat_wins_so_a_retried_error_replaces_the_failure(tmp_path):
    path = tmp_path / "r.jsonl"
    results_io.start_run(path, header())
    results_io.append_record(path, record("a", 0, actual={"error": "429"}))
    results_io.append_record(path, record("a", 0))
    run = results_io.read_run(path)
    assert len(run.records) == 1 and run.records[0]["actual"] == {"reply": "hi"}


def test_finished_keys_leave_out_errors_so_a_restart_retries_them(tmp_path):
    path = tmp_path / "r.jsonl"
    results_io.start_run(path, header())
    results_io.append_record(path, record("a", 0))
    results_io.append_record(path, record("b", 0, actual={"error": "429"}))
    assert results_io.finished_keys(path) == {("a", 0)}


def test_a_torn_last_line_is_ignored(tmp_path):
    path = tmp_path / "r.jsonl"
    results_io.start_run(path, header())
    results_io.append_record(path, record("a", 0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"case_id": "b", "repe')
    assert [r["case_id"] for r in results_io.read_run(path).records] == ["a"]


def test_resuming_checks_that_the_header_still_matches(tmp_path):
    path = tmp_path / "r.jsonl"
    results_io.start_run(path, header())
    results_io.check_resumable(path, header())
    with pytest.raises(results_io.ProvenanceError, match="prompt_version"):
        results_io.check_resumable(path, header(prompt_version="ffffffffffff"))
    with pytest.raises(results_io.ProvenanceError, match="dataset"):
        results_io.check_resumable(path, header(dataset_sha256="cd" * 32))


def test_models_and_totals_come_from_the_records(tmp_path):
    records = [record("a", 0, models=["m1", "m1"]), record("b", 0, models=["m1", "m2"], tokens={"prompt": 1, "completion": 2}, cost=0.5)]
    share = results_io.model_share(records)
    assert share == {"m1": 0.75, "m2": 0.25}
    totals = results_io.totals(records)
    assert totals == {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.5, "llm_calls": 4}
    assert results_io.is_mixed(records) is True
    assert results_io.is_mixed([record("a", 0)]) is False


def test_the_git_state_reports_a_sha_and_a_dirty_flag(tmp_path):
    sha, dirty = results_io.git_state()
    assert len(sha) >= 7 and isinstance(dirty, bool)
    assert results_io.git_state(tmp_path) == ("unknown", False)
