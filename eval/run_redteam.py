"""Red-team run: adversarial customer messages through the real webhook, the real model and real Odoo reads.

    python -m eval.run_redteam --domain cars --set originals --arm on --repeats 5
    python -m eval.run_redteam --domain cars --set both --arm off:tool_call_cap --targeted --repeats 5
    python -m eval.run_redteam --domain cars --set originals --arm all_off --repeats 5

Arms: `on` (every guardrail), `off:<name>` (one guardrail switched off, see eval.guardrails_off), `all_off`
(the negative control). With `--targeted`, an `off:<name>` arm runs only the attacks aimed at that guardrail.
Sets: `originals` (the 38 attacks), `variants` (paraphrases and other languages), `both`, `real_estate`
(the 38 reworded for the real-estate catalog; use with --domain real_estate).

Lead, contact, task and hold writes are recorded, not executed. Judging is done by `eval.redteam`
(independent detectors, deterministic). Results are resumable JSON lines after a provenance header.
"""
import argparse
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from engine.config import get_llm_client
from engine.core.agent_loop import PROMPT_VERSION
from engine.llm_client import Sampling
from eval import budget, cases, redteam, results_io
from eval.redteam import SETS, load_set, parse_arm

RESULTS_DIR = ROOT / "eval" / "results"
LEDGER_PATH = Path(os.environ.get("EVAL_LEDGER") or RESULTS_DIR / "call_ledger.json")  # a parallel run points this at its own file


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Run the LeadGate red-team attacks.")
    parser.add_argument("--domain", choices=["cars", "real_estate"], default="cars")
    parser.add_argument("--set", dest="set_name", choices=sorted(SETS), default="originals")
    parser.add_argument("--arm", default="on")
    parser.add_argument("--targeted", action="store_true", help="for off:<name>, run only the attacks aimed at that guardrail")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--only", type=int, nargs="*", help="run only these attack ids")
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument("--pace", type=float, default=3.0)
    parser.add_argument("--max-calls", type=int, default=4000)
    args = parser.parse_args(argv)
    off = parse_arm(args.arm)
    run_date = dt.date.today()
    results_dir = args.results_dir or RESULTS_DIR / f"{run_date.isoformat()}-redteam"
    items, dataset_name = load_set(args.set_name)
    if args.only:
        items = [c for c in items if c["id"] in set(args.only)]
    if args.targeted and off and len(off) == 1:
        items = redteam.targeted_cases(items, next(iter(off)))

    os.environ.setdefault("EVAL_RECORD_LEADS", "1")
    trace_path = str(Path(tempfile.gettempdir()) / f"leadgate_redteam_{os.getpid()}.jsonl")
    os.environ["TRACE_FILE"] = trace_path
    import engine.main as m

    llm = get_llm_client(m.config)
    models = [llm.model] if hasattr(llm, "model") else [c.model for c in getattr(llm, "clients", [])]
    budgeted = budget.BudgetedLLM(llm, LEDGER_PATH, cap=args.max_calls, label=f"redteam-{args.arm}", min_interval=args.pace)

    sampling = Sampling.from_env()
    sha, dirty = results_io.git_state()
    header = results_io.build_header(
        kind="redteam", domain=args.domain, dataset_name=dataset_name, dataset_sha256=cases.sha256_of(cases.DATASETS_DIR / SETS[args.set_name][0]),
        repeats=args.repeats, reference_date=run_date.isoformat(),
        sampling={"temperature": sampling.temperature, "max_tokens": sampling.max_tokens, "seed": sampling.seed},
        prompt_version=PROMPT_VERSION, models_configured=models, argv=["python", "-m", "eval.run_redteam", *argv],
        run_date=run_date.isoformat(), git_sha=sha, git_dirty=dirty, arm=args.arm, guardrails_off=sorted(off or []),
        targeted=args.targeted, n_attacks=len(items), llm_call_cap=args.max_calls,
    )
    out = Path(results_dir) / f"redteam_{args.domain}_{args.set_name}_{args.arm.replace(':', '-')}.jsonl"
    if out.exists():
        results_io.check_resumable(out, header)
    else:
        results_io.start_run(out, header)
    done = results_io.finished_keys(out)
    print(f"{len(items)} attacks x {args.repeats} repeats, arm {args.arm}, {args.domain}, models {models}; writing {out}")

    def stop() -> bool:
        return budgeted.daily_exhausted or budget.read_ledger(LEDGER_PATH).get("calls", 0) >= args.max_calls

    outcome = redteam.run_attacks(
        items, domain=args.domain, repeats=args.repeats, arm=args.arm, off=off, writer=lambda r: results_io.append_record(out, r),
        done=done, llm=budgeted, trace_path=trace_path, stop=stop,
    )
    clean = len(results_io.finished_keys(out))
    total = len(items) * args.repeats
    reason = "daily allowance or call cap reached" if outcome["stopped"] else None
    results_io.write_footer(out, {"complete": clean == total and not reason, "stopped": reason, "expected_records": total, "records_without_error": clean})
    print(f"{'COMPLETE' if clean == total and not reason else 'INCOMPLETE'}: {clean}/{total} attempts recorded. {reason or ''}")
    return 0 if clean == total and not reason else 2


if __name__ == "__main__":
    sys.exit(main())
