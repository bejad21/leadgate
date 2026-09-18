"""Eval runner for the LeadGate agent loop (Task 5.2).

Loads a labeled test-case dataset (Task 5.1) for a given domain, runs each
case through the real agent loop (`engine.core.agent_loop.run_turn`) against
a live Odoo instance and a real LLM (via OpenRouter's free tier), and writes
`{message, expected, actual, latency_ms}` records to
`eval/results/<domain>_results.json`.

This makes REAL network calls for every test case, so:
  - a small delay is inserted between cases to stay under free-tier
    per-minute rate limits
  - a 429 (rate limited) or transient network error triggers a couple of
    retries with backoff
  - if a case still fails after retries, the failure is recorded honestly
    in that case's result (actual.error) instead of crashing the whole run
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

# Delay between cases so we stay under the free-tier LLM's per-minute rate
# limit. Deliberately conservative given we're making ~2 LLM calls per case
# (the initial tool-selection call, plus a follow-up call to phrase the
# reply whenever a tool was actually invoked).
DELAY_BETWEEN_CASES_SECONDS = 2.0

# Retry policy for rate limiting / transient network failures. Kept small:
# this is a free-tier eval run, not a production service, and the brief
# calls for honestly recording a failure rather than looping forever.
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 20

RETRYABLE_NETWORK_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadTimeout)


def build_adapter(domain: str, config: dict):
    odoo = OdooClient(
        config["ODOO_URL"], config["ODOO_DB"], config["ODOO_USER"], config["ODOO_PASSWORD"]
    )
    if domain == "real_estate":
        return RealEstateAdapter(odoo)
    return CarsAdapter(odoo)


def run_turn_with_retry(history: list[dict], adapter, llm):
    """Run one turn, retrying on 429 / transient network errors.

    Returns (AgentTurnResult, None) on success, or (None, exception) if all
    attempts failed -- the caller records the exception as an honest failure
    instead of letting it crash the whole eval run.
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
