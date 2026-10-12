"""Eval runner for the LeadGate agent loop.

Loads a labeled test-case dataset for a domain, runs each case through the agent loop
(`engine.core.agent_loop.run_turn`) against a live Odoo instance and an LLM (OpenRouter's free
tier), and writes `{message, expected, actual, latency_ms}` records to
`eval/results/<domain>_results.json`.

Every case makes network calls, so:
- a short delay between cases keeps it under free-tier per-minute rate limits;
- a 429 or transient network error is retried a couple of times with backoff;
- a case that still fails is recorded in its result (`actual.error`) instead of crashing the run.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # Allow running this script directly (`python eval/run_eval.py`) without
    # requiring the project to be installed as a package.
    sys.path.insert(0, str(ROOT))

import httpx

from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.config import get_llm_client, load_config
from engine.core.agent_loop import run_turn
from engine.odoo_client import OdooClient

DATASETS = {
    "cars": ROOT / "eval" / "datasets" / "cars_test_set.json",
    "real_estate": ROOT / "eval" / "datasets" / "real_estate_test_set.json",
}
RESULTS_DIR = ROOT / "eval" / "results"

# Delay between cases to stay under the free-tier per-minute rate limit. Each case makes about two
# LLM calls: tool selection, then the reply once a tool ran.
DELAY_BETWEEN_CASES_SECONDS = 2.0

# Retries for rate limits and transient network failures. Kept small: a failure is recorded, not looped on.
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 20

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
    import os

    client_class = LeadRecordingOdoo if os.environ.get("EVAL_RECORD_LEADS") else OdooClient
    odoo = client_class(
        config["ODOO_URL"], config["ODOO_DB"], config["ODOO_USER"], config["ODOO_PASSWORD"]
    )
    if domain == "real_estate":
        return RealEstateAdapter(odoo)
    return CarsAdapter(odoo)


def run_turn_with_retry(history: list[dict], adapter, llm):
    """Run one turn, retrying on 429 and transient network errors.

    Returns (AgentTurnResult, None) on success, or (None, exception) if every attempt failed; the
    caller records the exception as a failure instead of crashing the run.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return run_turn(history, adapter, llm), None
        except httpx.HTTPStatusError as exc:
            last_error = exc
            is_rate_limited = exc.response.status_code == 429
            if is_rate_limited and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
                print(f"    rate limited (429) - retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            return None, exc
        except RETRYABLE_NETWORK_EXCEPTIONS as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
                print(f"    network error ({exc!r}) - retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            return None, exc
        except Exception as exc:  # noqa: BLE001 - deliberately broad: record, don't crash the run
            return None, exc
    return None, last_error


def run_case(case: dict, adapter, llm) -> dict:
    history = [{"role": "user", "content": case["message"]}]

    start = time.monotonic()
    turn_result, error = run_turn_with_retry(history, adapter, llm)
    latency_ms = (time.monotonic() - start) * 1000

    expected = {
        "tool": case.get("expected_tool"),
        "args": case.get("expected_args"),
        "outcome": case.get("expected_outcome"),
    }

    if error is not None:
        return {
            "message": case["message"],
            "expected": expected,
            "actual": {
                "error": f"{type(error).__name__}: {error}",
                "reply": None,
                "tool_calls_made": [],
                "tool_results": [],
            },
            "latency_ms": latency_ms,
        }

    return {
        "message": case["message"],
        "expected": expected,
        "actual": {
            "reply": turn_result.reply,
            # ToolCall is a dataclass, not natively JSON-serializable.
            "tool_calls_made": [asdict(tc) for tc in turn_result.tool_calls_made],
            "tool_results": turn_result.tool_results,
        },
        "latency_ms": latency_ms,
    }


def main():
    parser = argparse.ArgumentParser(description="Run the LeadGate agent loop against a labeled eval dataset.")
    parser.add_argument("--domain", required=True, choices=["cars", "real_estate"])
    args = parser.parse_args()

    config = load_config()
    llm = get_llm_client(config)
    adapter = build_adapter(args.domain, config)

    dataset_path = DATASETS[args.domain]
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    total = len(cases)

    print(f"Running {total} cases for domain={args.domain} against {llm.model} ...")

    results = []
    failures = 0
    run_start = time.monotonic()
    for i, case in enumerate(cases, start=1):
        print(f"[{i}/{total}] {case.get('id', '?')}: {case['message'][:70]!r}")
        result = run_case(case, adapter, llm)
        if "error" in result["actual"]:
            failures += 1
            print(f"    FAILED: {result['actual']['error']}")
        results.append(result)

        if i < total:
            time.sleep(DELAY_BETWEEN_CASES_SECONDS)

    wall_clock_s = time.monotonic() - run_start

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / f"{args.domain}_results.json"
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(
        f"Done: {len(results)}/{total} cases recorded ({failures} failed after retries) "
        f"in {wall_clock_s:.1f}s. Wrote {output_path}"
    )


if __name__ == "__main__":
    main()
