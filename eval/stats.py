"""Interval and repeat statistics for the eval: Wilson intervals, mean with interval, pass@1 and pass^k.

Pure arithmetic, no I/O. Every proportion the report prints goes through `wilson`, so a number never
appears without its interval.
"""

from __future__ import annotations

import math
import random
from collections.abc import Hashable, Iterable, Sequence

Z_95 = 1.959963984540054  # two-sided 95 % normal quantile


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float, float]:
    """(proportion, low, high): the Wilson score interval for `successes` out of `n`.

    With n == 0 there is nothing to estimate: (nan, nan, nan), never a fake 0 or 1.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got {successes}/{n}")
    if n == 0:
        return math.nan, math.nan, math.nan
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def mean_ci(values: Sequence[float], z: float = Z_95) -> tuple[float, float, float]:
    """(mean, low, high): normal-approximation interval for the mean of continuous values (latency, cost).

    One value gives a zero-width interval; no values give nan.
    """
    n = len(values)
    if n == 0:
        return math.nan, math.nan, math.nan
    mean = sum(values) / n
    if n == 1:
        return mean, mean, mean
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(variance / n)
    return mean, mean - half, mean + half


def case_bootstrap_interval(outcomes: Iterable[tuple[Hashable, bool]], resamples: int = 10_000, seed: int = 0, level: float = 0.95) -> tuple[float, float, float]:
    """(pass rate, low, high) with each CASE counted once, not each run.

    Repeats of one case are not independent tries (the same question tends to pass or fail the same way), so
    a Wilson interval over all runs is too narrow. Here every case gets its own pass rate over its repeats, the
    cases are resampled with replacement, and the interval is the middle `level` of the resampled means.
    The seed is fixed, so the same results always print the same interval. If all cases scored the same,
    resampling shows no spread, so the Wilson interval over the cases is used (30 of 30 does not print as 100 to 100).
    """
    per_case: dict[Hashable, list[bool]] = {}
    for case_id, passed in outcomes:
        per_case.setdefault(case_id, []).append(bool(passed))
    if not per_case:
        return math.nan, math.nan, math.nan
    rates = [sum(v) / len(v) for v in per_case.values()]
    mean = sum(rates) / len(rates)
    rng = random.Random(seed)
    n = len(rates)
    means = sorted(sum(rates[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    tail = (1 - level) / 2
    low, high = means[int(tail * resamples)], means[min(resamples - 1, int((1 - tail) * resamples))]
    if low == high:  # every case agreed: resampling cannot show any spread, so use the Wilson interval over cases
        _, low, high = wilson(round(mean * n), n)
    return mean, low, high


def pass_at_1_and_pass_k(outcomes: Iterable[tuple[Hashable, bool]], k: int) -> dict:
    """Repeat statistics from (case_id, passed) pairs, one pair per case and repeat.

    - pass@1: the mean pass rate over all runs (each (case, repeat) is one trial), with a Wilson interval
      over the trials.
    - pass^k: the share of cases that passed in ALL k of their runs, with a Wilson interval over cases.
      Only cases that have all k runs count; a case with fewer runs is reported in `n_incomplete_cases`
      and left out, so a half-finished run cannot flatter or punish the number.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    per_case: dict[Hashable, list[bool]] = {}
    for case_id, passed in outcomes:
        per_case.setdefault(case_id, []).append(bool(passed))
    trials = sum(len(v) for v in per_case.values())
    passes = sum(sum(v) for v in per_case.values())
    complete = {c: v for c, v in per_case.items() if len(v) >= k}
    all_pass = sum(1 for v in complete.values() if all(v[:k]))
    p1 = wilson(passes, trials)
    pk = wilson(all_pass, len(complete))
    by_case = case_bootstrap_interval((c, p) for c, v in per_case.items() for p in v)
    return {
        "k": k,
        "n_trials": trials,
        "n_cases": len(per_case),
        "pass_at_1": p1[0],
        "pass_at_1_low": p1[1],
        "pass_at_1_high": p1[2],
        "pass_at_1_case_low": by_case[1],
        "pass_at_1_case_high": by_case[2],
        "n_complete_cases": len(complete),
        "n_incomplete_cases": len(per_case) - len(complete),
        "pass_hat_k": pk[0],
        "pass_hat_k_low": pk[1],
        "pass_hat_k_high": pk[2],
    }


def format_interval(p: float, low: float, high: float, digits: int = 1) -> str:
    """'83.3% [68.1, 92.1]' style text; 'n/a' when there was nothing to estimate."""
    if math.isnan(p):
        return "n/a"
    return f"{p * 100:.{digits}f}% [{low * 100:.{digits}f}, {high * 100:.{digits}f}]"
