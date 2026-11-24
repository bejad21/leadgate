"""Write the sheet of 30 randomly chosen replies for the owner to hand-check against the automatic grounding verdicts.

    python -m eval.audit_sheet eval/results/<dir>/cars_heldout.jsonl [--n 30] [--seed 1] [--out sheet.csv]

The sample is repeatable for a given seed. The sheet lists the reply, the claims found, the automatic verdict
and two empty columns (owner_verdict, owner_notes). There is no LLM judge: the owner is the check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eval import claims, metrics, results_io


def build_sheet(path: Path, n: int, seed: int) -> str:
    run = results_io.read_run(path)
    graded = {(g["case_id"], g["repeat"]): g for g in metrics.grade_all(run.records)}
    records = [{**r, "grounding": {"claims": graded[(r["case_id"], r.get("repeat", 0))]["claims"]}} for r in run.records]
    return claims.audit_sheet(claims.sample_for_audit(records, n, seed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a hand-check sheet of random replies.")
    parser.add_argument("results_file", type=Path)
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        sheet = build_sheet(args.results_file, args.n, args.seed)
    except results_io.ProvenanceError as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 1
    out = args.out or args.results_file.with_name(f"audit_sheet_{args.results_file.stem}.csv")
    out.write_text(sheet, encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
