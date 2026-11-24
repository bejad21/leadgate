"""Grade every stored agent run again with the checker as it is now, and compare with an earlier grading.

No model call is made: a result file keeps the replies, the tool calls and the rules each case was run with, so
changing the checker means grading the same replies again. Use it to say what a checker change did.

    python -m eval.rescore --json before.json            # write the grading of every run as it is now
    python -m eval.rescore --compare before.json         # print before and after side by side
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval import metrics, report

ROOT = Path(__file__).resolve().parent.parent


def grade_all_runs(results_root: Path) -> dict[str, dict]:
    """label -> {passed, scored} for each complete agent run, by (case, repeat) records."""
    out: dict[str, dict] = {}
    for run in report.load_runs(results_root):
        if run.kind != "agent" or run.guardrails_off:
            continue
        graded = metrics.grade_all(run.records)
        scored = [g for g in graded if g["case_pass"] is not None]
        out[run.label] = {"passed": sum(1 for g in scored if g["case_pass"]), "scored": len(scored)}
    return out


def _pct(entry: dict) -> str:
    return f"{100 * entry['passed'] / entry['scored']:.1f}%" if entry["scored"] else "n/a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-root", type=Path, default=ROOT / "eval" / "results")
    parser.add_argument("--json", type=Path, help="write the current grading to this file")
    parser.add_argument("--compare", type=Path, help="an earlier --json file to compare with")
    args = parser.parse_args(argv)
    now = grade_all_runs(args.results_root)
    if args.json:
        args.json.write_text(json.dumps(now, indent=1), encoding="utf-8")
        print(f"wrote {len(now)} runs to {args.json}")
    if args.compare:
        before = json.loads(args.compare.read_text(encoding="utf-8"))
        print("| Run | Records | Passed before | Passed now | Before | Now |")
        print("|---|---|---|---|---|---|")
        for label in sorted(now):
            old, new = before.get(label), now[label]
            if old is None or (old["passed"], old["scored"]) == (new["passed"], new["scored"]):
                continue
            print(f"| {label} | {new['scored']} | {old['passed']} | {new['passed']} | {_pct(old)} | {_pct(new)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
