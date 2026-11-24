"""Summarise a turn-trace file (the JSON lines the engine writes when TRACE_FILE is set).

    python eval/trace_summary.py path/to/traces.jsonl

Prints the number of turns, mean and 95th percentile latency, mean tokens per turn, total and mean
cost, the share of turns that needed a failover or a retry round, and the cost per lead: the total
cost of the turns in which a `create_lead` call succeeded, divided by the number of those turns.
Lines that are not valid trace records are skipped and counted. When the traces carry a chat reference it also prints how
the conversations went: the share that stopped after one reply, the share that reached a request, how often a person was
asked for, and how often a button tap followed a reply with buttons.
"""

import argparse
import json
import math
import sys
from pathlib import Path

LEAD_TOOL = "create_lead"
# Any of these, finished without an error, is a customer who went all the way to asking for something.
REQUEST_TOOLS = ("create_lead", "reserve_item", "book_viewing")


def load_traces(path: Path) -> tuple[list[dict], int]:
    """The trace records in the file, and how many lines were skipped as unreadable."""
    traces: list[dict] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict) and "latency_ms" in record:
            traces.append(record)
        else:
            skipped += 1
    return traces, skipped


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value that at least `fraction` of the values do not exceed."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _made_a_lead(trace: dict) -> bool:
    return any(t.get("name") == LEAD_TOOL and t.get("outcome") == "ok" for t in trace.get("tools") or [])


def _made_a_request(trace: dict) -> bool:
    return any(t.get("name") in REQUEST_TOOLS and t.get("outcome") == "ok" for t in trace.get("tools") or [])


def conversation_metrics(traces: list[dict]) -> dict | None:
    """How the conversations went, from the traces alone (a trace keeps who and what happened, never the words). Chats are
    told apart by `chat_ref`, which is a keyed hash, not the Telegram id. None when no trace has a `chat_ref`.

    - stopped_after_first_reply: chats with a single customer turn. A chat that ends there got one answer and left.
    - chats_with_request: chats in which a lead, hold or viewing request was made.
    - turns_per_request_chat: the mean number of turns in those chats; fewer means the customer got there faster.
    - asked_for_person: chats where a message asked for a human, an agent or a callback.
    - button_turns: the share of turns that were a button tap, and tap_through the share of replies with buttons that was
      followed by a tap in the same chat."""
    chats: dict[str, list[dict]] = {}
    for trace in traces:
        ref = trace.get("chat_ref")
        if ref:
            chats.setdefault(ref, []).append(trace)
    if not chats:
        return None
    total_chats = len(chats)
    request_chats = [turns for turns in chats.values() if any(_made_a_request(t) for t in turns)]
    turn_total = sum(len(turns) for turns in chats.values())
    shown = [(ref, i) for ref, turns in chats.items() for i, t in enumerate(turns) if t.get("buttons_shown")]
    tapped = sum(1 for ref, i in shown if any(t.get("source") == "button" for t in chats[ref][i + 1 :]))
    return {
        "chats": total_chats,
        "stopped_after_first_reply": sum(1 for turns in chats.values() if len(turns) == 1) / total_chats,
        "chats_with_request": len(request_chats) / total_chats,
        "turns_per_request_chat": (sum(len(turns) for turns in request_chats) / len(request_chats)) if request_chats else None,
        "asked_for_person": sum(1 for turns in chats.values() if any(t.get("asked_for_person") for t in turns)) / total_chats,
        "button_turns": sum(1 for turns in chats.values() for t in turns if t.get("source") == "button") / turn_total,
        "replies_with_buttons": len(shown),
        "tap_through": (tapped / len(shown)) if shown else None,
    }


def summarize(traces: list[dict]) -> dict:
    turns = len(traces)
    if turns == 0:
        return {"turns": 0}
    latencies = [t.get("latency_ms", 0) for t in traces]
    tokens = [t.get("prompt_tokens", 0) + t.get("completion_tokens", 0) for t in traces]
    costs = [t.get("cost", 0.0) for t in traces]
    lead_costs = [t.get("cost", 0.0) for t in traces if _made_a_lead(t)]
    return {
        "turns": turns,
        "mean_latency_ms": sum(latencies) / turns,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "mean_tokens_per_turn": sum(tokens) / turns,
        "total_cost": sum(costs),
        "mean_cost_per_turn": sum(costs) / turns,
        "failover_share": sum(1 for t in traces if t.get("failover_used")) / turns,
        "retry_share": sum(1 for t in traces if t.get("retry_round_used")) / turns,
        "lead_turns": len(lead_costs),
        "cost_per_lead": sum(lead_costs) / len(lead_costs) if lead_costs else None,
        "conversation": conversation_metrics(traces),
    }


def format_summary(summary: dict, skipped: int = 0) -> str:
    if summary["turns"] == 0:
        return "No turns in the trace file."
    cost_per_lead = (
        f"{summary['cost_per_lead']:.6f} ({summary['lead_turns']} turn(s) with a lead)"
        if summary["cost_per_lead"] is not None
        else f"no turn created a lead ({LEAD_TOOL} was never called successfully)"
    )
    lines = [
        f"turns:                 {summary['turns']}",
        f"latency mean / p95:    {summary['mean_latency_ms']:.0f} ms / {summary['p95_latency_ms']:.0f} ms",
        f"mean tokens per turn:  {summary['mean_tokens_per_turn']:.1f}",
        f"cost total / mean:     {summary['total_cost']:.6f} / {summary['mean_cost_per_turn']:.6f}",
        f"failover share:        {summary['failover_share']:.0%}",
        f"retry round share:     {summary['retry_share']:.0%}",
        f"cost per lead:         {cost_per_lead}",
    ]
    talk = summary.get("conversation")
    if talk:
        per_request = f"{talk['turns_per_request_chat']:.1f}" if talk["turns_per_request_chat"] is not None else "n/a"
        tap_through = f"{talk['tap_through']:.0%}" if talk["tap_through"] is not None else "n/a (no replies had buttons)"
        lines += [
            f"chats:                 {talk['chats']}",
            f"stopped after 1 reply: {talk['stopped_after_first_reply']:.0%}",
            f"chats with a request:  {talk['chats_with_request']:.0%} (mean {per_request} turns each)",
            f"asked for a person:    {talk['asked_for_person']:.0%} of chats",
            f"button taps:           {talk['button_turns']:.0%} of turns; tap-through {tap_through}",
        ]
    if skipped:
        lines.append(f"skipped lines:         {skipped}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise a LeadGate turn-trace JSONL file.")
    parser.add_argument("trace_file", type=Path)
    args = parser.parse_args(argv)
    if not args.trace_file.is_file():
        print(f"No such file: {args.trace_file}", file=sys.stderr)
        return 1
    traces, skipped = load_traces(args.trace_file)
    print(format_summary(summarize(traces), skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
