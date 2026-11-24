import math

import pytest

from eval import stats


def test_wilson_matches_known_values():
    p, low, high = stats.wilson(8, 10)
    assert p == 0.8
    assert low == pytest.approx(0.4902, abs=1e-3)
    assert high == pytest.approx(0.9433, abs=1e-3)


def test_wilson_all_pass_and_none_pass_stay_inside_the_unit_interval():
    p, low, high = stats.wilson(10, 10)
    assert p == 1.0 and high == pytest.approx(1.0) and low == pytest.approx(0.7225, abs=1e-3)
    p, low, high = stats.wilson(0, 10)
    assert p == 0.0 and low == pytest.approx(0.0, abs=1e-12) and high == pytest.approx(0.2775, abs=1e-3)


def test_wilson_with_no_trials_is_nan_not_a_fake_zero():
    assert all(math.isnan(x) for x in stats.wilson(0, 0))


def test_wilson_rejects_impossible_counts():
    with pytest.raises(ValueError):
        stats.wilson(5, 4)
    with pytest.raises(ValueError):
        stats.wilson(-1, 4)


def test_mean_ci():
    mean, low, high = stats.mean_ci([1, 2, 3, 4, 5])
    assert mean == 3
    assert low == pytest.approx(3 - 1.96 * math.sqrt(2.5 / 5), abs=1e-3)
    assert high == pytest.approx(3 + 1.96 * math.sqrt(2.5 / 5), abs=1e-3)
    assert stats.mean_ci([7]) == (7, 7, 7)
    assert math.isnan(stats.mean_ci([])[0])


def test_pass_k_is_the_share_of_cases_that_pass_every_run():
    outcomes = [("a", True), ("a", True), ("b", True), ("b", False), ("c", False), ("c", False)]
    result = stats.pass_at_1_and_pass_k(outcomes, 2)
    assert result["pass_at_1"] == pytest.approx(3 / 6)
    assert result["pass_hat_k"] == pytest.approx(1 / 3)
    assert result["n_trials"] == 6 and result["n_cases"] == 3
    assert result["pass_hat_k_low"] == pytest.approx(stats.wilson(1, 3)[1])


def test_pass_k_leaves_out_cases_with_fewer_than_k_runs():
    outcomes = [("a", True), ("a", True), ("b", True)]
    result = stats.pass_at_1_and_pass_k(outcomes, 2)
    assert result["n_complete_cases"] == 1 and result["n_incomplete_cases"] == 1
    assert result["pass_hat_k"] == 1.0
    assert result["pass_at_1"] == 1.0


def test_pass_k_needs_a_positive_k():
    with pytest.raises(ValueError):
        stats.pass_at_1_and_pass_k([], 0)


def test_format_interval():
    assert stats.format_interval(*stats.wilson(8, 10)) == "80.0% [49.0, 94.3]"
    assert stats.format_interval(*stats.wilson(0, 0)) == "n/a"


def test_case_interval_is_wider_than_the_run_interval_when_repeats_agree():
    outcomes = [(f"c{i}", i >= 3) for i in range(30) for _ in range(5)]
    result = stats.pass_at_1_and_pass_k(outcomes, 5)
    assert result["pass_at_1_low"] > 0.77
    assert result["pass_at_1_case_low"] < result["pass_at_1_low"]
    assert result["pass_at_1_case_high"] > result["pass_at_1_high"] - 0.02


def test_case_interval_is_fixed_by_its_seed_and_handles_no_cases():
    outcomes = [("a", True), ("a", False), ("b", True), ("b", True)]
    assert stats.case_bootstrap_interval(outcomes) == stats.case_bootstrap_interval(outcomes)
    mean, low, high = stats.case_bootstrap_interval([])
    assert mean != mean and low != low and high != high


def test_case_interval_is_not_zero_width_when_every_case_passes():
    mean, low, high = stats.case_bootstrap_interval([(f"c{i}", True) for i in range(30) for _ in range(2)])
    assert mean == 1.0 and low < 0.9 and high > 0.999
