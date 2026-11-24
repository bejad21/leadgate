"""Eval runner for the LeadGate agent loop.

    python -m eval.run_eval --domain cars --set heldout --repeats 5 [--results-dir DIR]

Runs every case of a dataset through the real agent loop (`engine.core.agent_loop.run_turn`), the real
model and Odoo reads, `--repeats` times, and appends one JSON line per (case, repeat) to
`<results-dir>/<domain>_<set>.jsonl` after a provenance header (see `eval.results_io`).

- Resumable: a restart skips every (case, repeat) already recorded without an error and redoes the rest.
- Repeat-major order: all cases run once before any runs twice, so a run cut short still has a full pass@1.
- Paced and capped: calls are spaced out and counted in a ledger (`eval.budget`); a 429 is waited out, and
  an exhausted daily allowance or call cap ends the run cleanly with the file marked incomplete.
- Held-out labels with placeholders are resolved against the live catalog and a fixed reference date, which
  is also the date the model is told is "today", and stored in each record.
- Writes are recorded, not executed, when EVAL_RECORD_LEADS=1 (see LeadRecordingOdoo).
"""

import argparse
import copy
import datetime as dt
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from engine import tracing
from engine.adapters import ADAPTERS
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import PROMPT_VERSION, run_turn
from engine.llm_client import Sampling
from engine.odoo_client import OdooClient
from eval import baselines, budget, cases, results_io

RESULTS_DIR = ROOT / "eval" / "results"
LEDGER_PATH = Path(os.environ.get("EVAL_LEDGER") or RESULTS_DIR / "call_ledger.json")  # a parallel run points this at its own file
LLM_CALL_CAP = 4000
SEARCH_TOOL = {"cars": "search_inventory", "real_estate": "search_listings"}

# Pause between cases; BudgetedLLM paces the calls themselves.
DELAY_BETWEEN_CASES_SECONDS = 0.0
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 20
MAX_WAIT_SECONDS = 180

RETRYABLE_NETWORK_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadTimeout)


class LeadRecordingOdoo(OdooClient):
    """Real reads; lead, contact and task creation, field writes and method calls (holds, status changes)
    are answered with a fake result instead of being executed, so re-running the eval does not fill the
    CRM with leads or put items on hold. Other creates (such as tags) still run. Enabled with
    EVAL_RECORD_LEADS=1.
    """

    def create(self, model, values):
        if model in ("crm.lead", "res.partner", "mail.activity"):
            return 900_000
        return super().create(model, values)

    def write(self, model, ids, values):
        return True  # no status, stage or link is changed for real

    def call(self, model, method, args, kwargs=None):
        if model == "leadgate.catalog.item" and method == "action_reserve":
            return {"ok": True, "reserved_until": "2999-01-01 00:00:00"}  # a hold is answered, not placed
        return True


def build_adapter(domain: str, config: dict):
    client_class = LeadRecordingOdoo if os.environ.get("EVAL_RECORD_LEADS") else OdooClient
    odoo = client_class(config["ODOO_URL"], config["ODOO_DB"], config["ODOO_USER"], config["ODOO_PASSWORD"])
    return ADAPTERS[domain](odoo)


def traced_turn(history: list[dict], adapter, llm):
    """run_turn, plus one trace line per attempt (to the log and to TRACE_FILE when it is set), so the
    eval measures tokens, cost and latency the same way the engine does. `chat_ref` is "eval": there is no chat."""
    trace = tracing.TurnTrace(chat_ref="eval")
    try:
        result = run_turn(history, adapter, llm, trace=trace)
    except Exception:
        trace.outcome = "error"
        tracing.emit(trace)
        raise
    tracing.emit(trace)
    return result


def run_turn_with_retry(history: list[dict], adapter, llm, *, sleep=time.sleep, max_retries: int = MAX_RETRIES):
    """Run one turn, waiting out 429s (as long as the provider says, capped) and retrying network errors.

    Returns (AgentTurnResult, None) on success or (None, exception) when every attempt failed. A spent
    daily allowance raises DailyAllowanceExhausted, and BudgetExhausted passes through, so the caller can stop.
    """
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        # Every attempt starts from the conversation as it was. A turn that failed after its tool ran has already
        # appended that call and result to the list it was given, and a retry that saw them would answer without
        # calling a tool, which then scores as a wrong tool choice.
        attempt_history = copy.deepcopy(history)
        try:
            result = traced_turn(attempt_history, adapter, llm)
            history[:] = attempt_history
            return result, None
        except (budget.BudgetExhausted, budget.DailyAllowanceExhausted):
            raise
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code != 429 or attempt >= max_retries:
                return None, exc
            info = budget.parse_rate_limit(exc.response)
            if info.daily_exhausted:
                raise budget.DailyAllowanceExhausted(f"the provider says the day's allowance is gone (limit {info.limit})") from exc
            sleep(min(max(info.wait_seconds, 1.0), MAX_WAIT_SECONDS))
        except RETRYABLE_NETWORK_EXCEPTIONS as exc:
            last_error = exc
            if attempt >= max_retries:
                return None, exc
            sleep(RETRY_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: record, don't crash the run
            return None, exc
    return None, last_error


def load_catalog(adapter, domain: str) -> list[dict]:
    """The items a customer can see (status available), with ids, for resolving labels and rebuilding history."""
    return adapter.odoo.search_read(
        "leadgate.catalog.item", [("domain_type", "=", domain), ("status", "=", "available")], ["name", "price", "attributes"]
    )


def run_case(case: dict, adapter, llm, *, catalog: list[dict], reference: dt.date, repeat: int, domain: str, set_name: str) -> dict:
    expected = cases.build_expected(case, catalog, reference)
    history = [dict(m) for m in cases.build_history(case.get("history") or [], catalog, SEARCH_TOOL[domain])]
    history.append({"role": "user", "content": case["message"]})

    start = time.monotonic()
    turn, error = run_turn_with_retry(history, adapter, llm)
    latency_ms = (time.monotonic() - start) * 1000
    record = {
        "case_id": case["id"], "category": case.get("category"), "set": set_name, "domain": domain, "repeat": repeat,
        "message": case["message"], "history": case.get("history"), "expected": expected, "latency_ms": latency_ms,
        "models": [], "tokens": {"prompt": 0, "completion": 0}, "cost": 0.0,
    }
    if error is not None or turn is None:
        record["actual"] = {"error": f"{type(error).__name__}: {error}", "reply": None, "tool_calls_made": [], "tool_results": []}
        return record
    trace = turn.trace
    if trace is not None:
        record["models"] = list(trace.models)
        record["tokens"] = {"prompt": trace.prompt_tokens, "completion": trace.completion_tokens}
        record["cost"] = trace.cost
        record["failover_used"] = trace.failover_used
    record["actual"] = {
        "reply": turn.reply,
        "tool_calls_made": [asdict(tc) for tc in turn.tool_calls_made],
        "tool_results": turn.tool_results,
    }
    return record


def run_dataset(items, adapter, llm, *, catalog, reference, path: Path, header: dict, domain: str, set_name: str,
                repeats: int, delay: float = 0.0, sleep=time.sleep, log=print, runner=None) -> dict:
    """Run every (repeat, case) not yet recorded.

    Returns {"complete", "reason", "recorded", "failed", "total"}. A run is complete only when every
    (case, repeat) has a record without an error.
    """
    if path.exists():
        results_io.check_resumable(path, header)
    else:
        results_io.start_run(path, header)
    runner = runner or run_case
    done = results_io.finished_keys(path)
    total = repeats * len(items)
    recorded = failed = 0
    reason = None
    try:
        for repeat in range(repeats):
            for case in items:
                if (case["id"], repeat) in done:
                    continue
                record = runner(case, adapter, llm, catalog=catalog, reference=reference, repeat=repeat, domain=domain, set_name=set_name)
                results_io.append_record(path, record)
                recorded += 1
                if record["actual"].get("error"):
                    failed += 1
                    log(f"  {case['id']} repeat {repeat}: FAILED {record['actual']['error'][:120]}")
                if delay:
                    sleep(delay)
    except (budget.BudgetExhausted, budget.DailyAllowanceExhausted) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    clean = len(results_io.finished_keys(path))
    complete = reason is None and clean == total
    results_io.write_footer(path, {"complete": complete, "stopped": reason, "expected_records": total, "records_without_error": clean})
    return {"complete": complete, "reason": reason, "recorded": recorded, "failed": failed, "total": total}


def freeze_today(reference: dt.date) -> None:
    """Tell the model the reference date is today, so 'next Saturday' means the same thing on every run."""
    import engine.core.agent_loop as loop

    loop.system_prompt = lambda: f"{loop.SYSTEM_PROMPT}\n\nToday's date is {reference.isoformat()}."


def guardrail_names(flag: str | None) -> set[str]:
    """Guardrails named by --guardrails-off: nothing for None, every one for "all", else a comma list."""
    from eval.guardrails_off import GUARDRAILS

    if not flag:
        return set()
    return set(GUARDRAILS) if flag == "all" else {n.strip() for n in flag.split(",") if n.strip()}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Run the LeadGate agent loop against a labeled eval dataset.")
    parser.add_argument("--domain", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--set", dest="set_name", choices=["dev", "heldout", "fresh", "fresh2", "fresh3", "fresh4", "fresh5", "fresh6dev", "fresh6test", "fresh7dev", "fresh7test"], default="dev")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, help="run only the first N cases (a smoke run)")
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument("--reference-date", default=None, help="YYYY-MM-DD, default today")
    parser.add_argument("--pace", type=float, default=3.0, help="minimum seconds between LLM calls")
    parser.add_argument("--max-calls", type=int, default=LLM_CALL_CAP)
    parser.add_argument("--baseline", choices=["router", "notools"], default=None,
                        help="score a baseline instead of the agent: a keyword router (no model) or the model without tools")
    parser.add_argument("--guardrails-off", default=None, help="'all' or a comma list: run the agent with those guardrails off (benign over-blocking check)")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    smoke = args.limit is not None
    run_date = dt.date.today()
    if args.results_dir is None:
        args.results_dir = (
            Path(tempfile.gettempdir()) / "leadgate_eval_smoke" if smoke else RESULTS_DIR / f"{run_date.isoformat()}-{args.set_name}"
        )
    resolved = args.results_dir.resolve()
    if smoke and (resolved == RESULTS_DIR.resolve() or RESULTS_DIR.resolve() in resolved.parents):
        parser.error("--limit writes a partial result, so it may not go under eval/results; pick another --results-dir")
    reference = dt.date.fromisoformat(args.reference_date) if args.reference_date else run_date

    if args.set_name == "heldout":
        cases.verify_frozen()
    elif args.set_name in ("fresh", "fresh2", "fresh3", "fresh4", "fresh5", "fresh6dev", "fresh6test", "fresh7dev", "fresh7test"):
        cases.verify_frozen(hash_file=cases.HASH_FILES[args.set_name])
    config = load_config()
    llm = get_llm_client(config)
    models = [llm.model] if hasattr(llm, "model") else [c.model for c in getattr(llm, "clients", [])]
    llm = budget.BudgetedLLM(llm, LEDGER_PATH, cap=args.max_calls, label=f"{args.domain}-{args.set_name}", min_interval=args.pace)
    adapter = build_adapter(args.domain, config)
    path_data = cases.dataset_path(args.domain, args.set_name)
    _, items = cases.load_dataset(path_data)
    items = items[: args.limit]
    catalog = load_catalog(adapter, args.domain)
    freeze_today(reference)
    switched_off = guardrail_names(args.guardrails_off)
    if switched_off:
        import engine.main as engine_main
        from eval.guardrails_off import disable_guardrails

        disable_guardrails(engine_main, only=switched_off)

    runner, kind, prompt_version = None, "agent", PROMPT_VERSION
    if args.baseline == "router":
        router = baselines.KeywordRouter(args.domain, catalog)
        runner = lambda case, adapter_, llm_, **kw: baselines.run_router_case(case, router, adapter_, **kw)  # noqa: E731
        kind, models, prompt_version = "keyword_router", [baselines.ROUTER_MODEL], "keyword-router-v1"
    elif args.baseline == "notools":
        runner = baselines.run_no_tools_case
        kind, prompt_version = "no_tools_llm", tracing.prompt_version(baselines.no_tools_prompt(args.domain, catalog))

    sha, dirty = results_io.git_state()
    sampling = Sampling.from_env()
    header = results_io.build_header(
        kind=kind, domain=args.domain, dataset_name=path_data.name, dataset_sha256=cases.sha256_of(path_data),
        repeats=args.repeats, reference_date=reference.isoformat(),
        sampling={"temperature": sampling.temperature, "max_tokens": sampling.max_tokens, "seed": sampling.seed, "reasoning_effort": sampling.reasoning_effort},
        prompt_version=prompt_version, models_configured=models, argv=["python", "-m", "eval.run_eval", *argv],
        run_date=run_date.isoformat(), git_sha=sha, git_dirty=dirty, n_cases=len(items), llm_call_cap=args.max_calls, guardrails_off=sorted(switched_off),
    )
    suffix = (f"_{args.baseline}" if args.baseline else "") + ("_guardrails-off" if switched_off else "")
    out = args.results_dir / f"{args.domain}_{args.set_name}{suffix}.jsonl"
    print(f"{len(items)} cases x {args.repeats} repeats, {args.domain} {args.set_name}, models {models}; writing {out}")
    outcome = run_dataset(
        items, adapter, llm, catalog=catalog, reference=reference, path=out, header=header,
        domain=args.domain, set_name=args.set_name, repeats=args.repeats, delay=DELAY_BETWEEN_CASES_SECONDS, runner=runner,
    )
    status = "COMPLETE" if outcome["complete"] else f"INCOMPLETE ({outcome['reason'] or 'records missing or failed'})"
    print(f"{status}: {outcome['recorded']} new records, {outcome['failed']} failed, expected {outcome['total']}. Wrote {out}")
    return 0 if outcome["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
