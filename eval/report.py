"""One command that regenerates every table and chart from the stored result files.

    python -m eval.report

Reads only `eval/results/*/*.jsonl` (each with a provenance header; a file without one stops the command),
recomputes every number with `eval.metrics` and `eval.redteam`, rewrites the tables in eval/REPORT.md
between `<!-- BEGIN:name -->` / `<!-- END:name -->` markers, and writes the charts. No number is typed by hand.
The older single-run files in eval/results/ (no header) are ignored: they are the "superseded" section.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import budget, metrics, redteam, results_io, stats

RESULTS_ROOT = ROOT / "eval" / "results"
REPORT_PATH = ROOT / "eval" / "REPORT.md"
CHARTS_DIR = ROOT / "eval" / "charts"
TABLE_NAMES = [
    "headline-fresh", "baselines-fresh", "headline-heldout", "dev", "baselines", "redteam", "benign", "provenance", "calls",
]
KIND_LABEL = {"agent": "agent", "keyword_router": "keyword router", "no_tools_llm": "no-tools LLM"}


@dataclass
class LoadedRun:
    path: Path
    header: dict
    records: list[dict]
    footer: dict | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def kind(self) -> str:
        return self.header["kind"]

    @property
    def is_heldout(self) -> bool:
        return self.header["dataset"]["name"].startswith("heldout")

    @property
    def dataset_kind(self) -> str:
        """heldout, fresh (every fresh set) or dev, from the dataset's file name."""
        name = self.header["dataset"]["name"]
        return "heldout" if name.startswith("heldout") else "fresh" if name.startswith("fresh") else "dev"

    @property
    def label(self) -> str:
        """The result file as a reader finds it: folder and file name, which tell two runs of one set apart."""
        return f"{self.path.parent.name}/{self.path.name}"

    @property
    def guardrails_off(self) -> list[str]:
        return list(self.header.get("guardrails_off") or [])

    @property
    def status(self) -> str:
        done = bool(self.footer and self.footer.get("complete"))
        return "complete" if done else "INCOMPLETE"


def load_runs(root: Path) -> list[LoadedRun]:
    """Every results file under root/<dir>/, validated. Raises ProvenanceError on a file without a header."""
    runs = []
    for path in sorted(Path(root).glob("*/*.jsonl")):
        run = results_io.read_run(path)
        runs.append(LoadedRun(path, run.header, run.records, run.footer))
    return runs


def fmt(prop: dict) -> str:
    if not prop["n"]:
        return "n/a"
    return f"{stats.format_interval(prop['p'], prop['low'], prop['high'])} ({prop['successes']}/{prop['n']})"


def _models(run: LoadedRun) -> str:
    share = results_io.model_share(run.records)
    if not share:
        return ", ".join(run.header["model_configured"])
    return ", ".join(f"{m} ({s:.0%})" for m, s in share.items())


def _summary(run: LoadedRun) -> dict:
    return metrics.summarize_records(metrics.grade_all(run.records), int(run.header["repeats"]))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No result files yet._"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _flags(run: LoadedRun) -> str:
    flags = [run.status]
    if "partial" in run.path.parent.name:
        flags.append("SUPERSEDED: other model, partial, not comparable")
    if "first-attempt" in run.path.parent.name:
        flags.append("SUPERSEDED: the retry bug put a failed attempt's tool call into the retry, so correct answers scored as no tool")
    if "truncated" in run.path.parent.name:
        flags.append("SUPERSEDED: the model used its whole token budget thinking, many empty replies")
    if results_io.is_mixed(run.records):
        flags.append("MIXED MODELS")
    if run.header["git_dirty"]:
        flags.append("dirty tree")
    return ", ".join(flags)


def render_agent_table(runs: list[LoadedRun], set_name: str) -> str:
    rows = []
    for run in runs:
        if run.kind != "agent" or run.guardrails_off or run.dataset_kind != set_name:
            continue
        s = _summary(run)
        pk = {"successes": round(s["pass_k"]["pass_hat_k"] * s["pass_k"]["n_complete_cases"]) if s["pass_k"]["n_complete_cases"] else 0,
              "n": s["pass_k"]["n_complete_cases"], "p": s["pass_k"]["pass_hat_k"], "low": s["pass_k"]["pass_hat_k_low"], "high": s["pass_k"]["pass_hat_k_high"]}
        p1 = {"successes": s["case_pass"]["successes"], "n": s["case_pass"]["n"], "p": s["pass_k"]["pass_at_1"],
              "low": s["pass_k"]["pass_at_1_low"], "high": s["pass_k"]["pass_at_1_high"]}
        rows.append([
            run.header["domain"], f"`{run.label}`", _models(run), f"{s['pass_k']['n_cases']} cases", str(s["n_records"]),
            fmt(s["tool_selection"]), fmt(s["slot_extraction"]), fmt(s["outcome"]), fmt(s["grounding"]), fmt(p1), _case_range(s), fmt(pk),
            f"{s['n_errors']} errors, {s['n_unresolvable']} unresolvable", _flags(run),
        ])
    repeats = {int(r.header["repeats"]) for r in runs if r.kind == "agent" and not r.guardrails_off and r.dataset_kind == set_name}
    pass_k = f"case pass^{repeats.pop()}" if len(repeats) == 1 else "case pass^k (k = repeats)"
    headers = ["domain", "file", "model (share of calls)", "n", "records", "tool selection", "slot extraction", "outcome rules",
               "claim grounding", "case pass@1", "range over cases", pass_k, "excluded", "status"]
    return _table(headers, rows)


def _case_range(summary: dict) -> str:
    """The interval that counts each case once; wider than the run-level Wilson interval next to it."""
    k = summary["pass_k"]
    return f"{k['pass_at_1_case_low'] * 100:.1f} to {k['pass_at_1_case_high'] * 100:.1f}" if k["n_cases"] else "n/a"


def render_baseline_table(runs: list[LoadedRun], set_name: str = "heldout") -> str:
    rows = []
    order = {"agent": 0, "keyword_router": 1, "no_tools_llm": 2}
    for run in sorted(runs, key=lambda r: (r.header["domain"], r.dataset_kind, order.get(r.kind, 9), r.label)):
        if run.dataset_kind != set_name or run.guardrails_off or run.kind not in order:
            continue
        s = _summary(run)
        p1 = {"successes": s["case_pass"]["successes"], "n": s["case_pass"]["n"], "p": s["pass_k"]["pass_at_1"],
              "low": s["pass_k"]["pass_at_1_low"], "high": s["pass_k"]["pass_at_1_high"]}
        rows.append([run.header["domain"], KIND_LABEL[run.kind], f"`{run.label}`", _models(run), fmt(s["tool_selection"]),
                     fmt(s["outcome"]), fmt(s["grounding"]), fmt(p1), _flags(run)])
    headers = ["domain", "system", "file", "model", "tool selection", "outcome rules", "claim grounding", "case pass@1", "status"]
    return _table(headers, rows)


def _group_redteam(runs: list[LoadedRun]) -> dict:
    groups: dict[tuple, dict[str, LoadedRun]] = {}
    for run in runs:
        if run.kind == "redteam":
            groups.setdefault((run.header["domain"], run.header["dataset"]["name"]), {})[run.header["arm"]] = run
    return groups


def render_redteam_table(runs: list[LoadedRun]) -> str:
    rows = []
    on_rows: dict[str, list[dict]] = {}
    for run in runs:
        if run.kind == "redteam" and run.header["arm"] == "on":
            on_rows.setdefault(run.header["domain"], []).extend(run.records)
    for (domain, dataset), arms in sorted(_group_redteam(runs).items()):
        for arm, run in sorted(arms.items(), key=lambda kv: (kv[0] != "on", kv[0])):
            rate = redteam.attack_rate(run.records)
            ids = {r["case_id"] for r in run.records}
            same_rows = [r for r in on_rows.get(domain, []) if r["case_id"] in ids]
            same = redteam.attack_rate(same_rows) if same_rows and arm != "on" else None
            rows.append([
                domain, dataset, arm, str(len(ids)), str(rate["n"]), str(rate["inconclusive"]), fmt(rate),
                fmt(same) if same else ("(this is the baseline)" if arm == "on" else "no guardrails-on run of these attacks"), _flags(run),
            ])
    return _table(["domain", "attack set", "arm", "attacks", "conclusive attempts", "inconclusive", "attack success", "attack success, guardrails on, same attacks", "status"], rows)


def render_benign_table(runs: list[LoadedRun]) -> str:
    rows = []
    for run in sorted(runs, key=lambda r: (r.header["domain"], bool(r.guardrails_off))):
        if run.kind != "agent" or run.is_heldout:
            continue
        s = _summary(run)
        label = "off: " + ", ".join(run.guardrails_off) if run.guardrails_off else "on"
        p1 = {"successes": s["case_pass"]["successes"], "n": s["case_pass"]["n"], "p": s["pass_k"]["pass_at_1"],
              "low": s["pass_k"]["pass_at_1_low"], "high": s["pass_k"]["pass_at_1_high"]}
        rows.append([run.header["domain"], label if len(label) < 40 else "off: all", _models(run), fmt(s["tool_selection"]), fmt(s["outcome"]), fmt(p1), _flags(run)])
    return _table(["domain", "guardrails", "model", "tool selection", "outcome rules", "case pass@1", "status"], rows)


def render_provenance_table(runs: list[LoadedRun]) -> str:
    rows = []
    for run in runs:
        h = run.header
        t = results_io.totals(run.records)
        rows.append([
            f"`{run.path.parent.name}/{run.name}`", KIND_LABEL.get(h["kind"], h["kind"]), h["run_date"],
            f"{h['git_sha']}{' (dirty)' if h['git_dirty'] else ''}", f"`{h['prompt_version']}`", f"`{h['dataset']['sha256'][:12]}`",
            f"T={h['sampling']['temperature']}, max_tokens={h['sampling']['max_tokens']}, seed={h['sampling']['seed']}",
            h["reference_date"], str(h["repeats"]), _models(run),
            f"{t['prompt_tokens']:,} / {t['completion_tokens']:,}", f"{t['cost']:.4f}", f"`{h['command']}`", run.status,
        ])
    return _table(["file", "kind", "run date", "git sha", "prompt hash", "dataset sha256", "sampling", "reference date", "repeats",
                   "models (share of calls)", "tokens in / out", "est. cost", "command", "status"], rows)


def render_calls(root: Path) -> str:
    ledger = budget.read_ledger(Path(root) / "call_ledger.json")
    by = ", ".join(f"{k}: {v}" for k, v in sorted((ledger.get("by_label") or {}).items())) or "none"
    text = f"Live LLM calls counted in `eval/results/call_ledger.json`: **{ledger.get('calls', 0)}** of the hard cap of 4000 (runs were limited to 3850 to keep a reserve) ({by})."
    per_run = [budget.read_ledger(p).get("calls", 0) for p in sorted(Path(root).glob("ledgers-*/*.json"))]
    if per_run:
        text += (
            f"\n\nRuns made in parallel on separate API keys each kept their own ledger in `eval/results/ledgers-*/`: "
            f"{len(per_run)} ledgers, **{sum(per_run)}** further calls in all."
        )
    return text


def update_markers(text: str, tables: dict[str, str]) -> str:
    for name, body in tables.items():
        begin, end = f"<!-- BEGIN:{name} -->", f"<!-- END:{name} -->"
        if begin not in text or end not in text:
            raise KeyError(f"REPORT.md has no markers for {name}")
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        text = f"{head}{begin}\n{body}\n{end}{tail}"
    return text


def write_charts(runs: list[LoadedRun], out_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    colors = {"cars": "#2A78D6", "real_estate": "#EC6A36", "other": "#7A7A74"}

    latest: dict[str, LoadedRun] = {}  # the newest complete held-out run of each domain; older ones stay in the tables
    for r in sorted(runs, key=lambda r: (r.header["run_date"], r.path.parent.name)):
        if r.kind == "agent" and r.is_heldout and not r.guardrails_off and r.status == "complete" and not r.header["git_dirty"]:
            latest[r.header["domain"]] = r
    agents = list(latest.values())
    if agents:
        labels = ["Tool\nselection", "Slot\nextraction", "Outcome\nrules", "Claim\ngrounding", "Case\npass"]
        fig, ax = plt.subplots(figsize=(11, 6.2), dpi=150)
        width = 0.8 / len(agents)
        for i, run in enumerate(agents):
            s = _summary(run)
            props = [s["tool_selection"], s["slot_extraction"], s["outcome"], s["grounding"], s["case_pass"]]
            xs = [j + (i - (len(agents) - 1) / 2) * width for j in range(len(labels))]
            values = [p["p"] if p["n"] else 0 for p in props]
            err = [[max(0, p["p"] - p["low"]) if p["n"] else 0 for p in props], [max(0, p["high"] - p["p"]) if p["n"] else 0 for p in props]]
            ax.bar(xs, values, width, yerr=err, capsize=3, color=colors.get(run.header["domain"], colors["other"]),
                   label=f"{run.header['domain']} held-out, {s['pass_k']['n_cases']} cases x {run.header['repeats']} ({_flags(run)})")
        ax.set_xticks(range(len(labels)), labels)
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("Share of runs (95% Wilson interval)")
        ax.set_title("Held-out results, one bar per domain")
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.28), frameon=False, fontsize=9)
        fig.tight_layout()
        path = out_dir / "heldout_metrics.png"
        fig.savefig(path)
        plt.close(fig)
        written.append(path)

    fresh = [r for r in runs if r.kind == "agent" and r.dataset_kind == "fresh" and not r.guardrails_off and r.status == "complete"]
    if fresh:
        fresh.sort(key=lambda r: (r.header["dataset"]["name"], r.header["domain"], r.path.parent.name))
        bars = []
        for r in fresh:
            p = _summary(r)["case_pass"]
            engine = "old engine" if "-old" in r.path.parent.name else "new engine"
            bars.append((f"{r.header['dataset']['name'].split('_')[0]}\n{r.header['domain']}\n{engine}", p, "#7A7A74" if "-old" in r.path.parent.name else colors.get(r.header["domain"], colors["other"])))
        fig, ax = plt.subplots(figsize=(max(8, 0.9 * len(bars)), 5.5), dpi=150)
        xs = range(len(bars))
        ax.bar(xs, [p["p"] for _, p, _ in bars], yerr=[[max(0.0, p["p"] - p["low"]) for _, p, _ in bars], [max(0.0, p["high"] - p["p"]) for _, p, _ in bars]],
               capsize=3, color=[c for _, _, c in bars])
        ax.set_xticks(list(xs), [label for label, _, _ in bars], fontsize=8)
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("Case pass rate (95% Wilson interval)")
        ax.set_title("Unseen sets: old engine against new engine")
        fig.tight_layout()
        path = out_dir / "fresh_case_pass.png"
        fig.savefig(path)
        plt.close(fig)
        written.append(path)

    groups = _group_redteam(runs)
    if groups:
        points = []
        for (domain, _dataset), arms in sorted(groups.items()):
            for arm, run in sorted(arms.items()):
                rate = redteam.attack_rate(run.records)
                if rate["n"]:
                    points.append((f"{domain}\n{arm}", rate))
        if points:
            fig, ax = plt.subplots(figsize=(max(8, 0.9 * len(points)), 5.5), dpi=150)
            xs = range(len(points))
            ax.bar(xs, [p["p"] for _, p in points], yerr=[[max(0.0, p["p"] - p["low"]) for _, p in points], [max(0.0, p["high"] - p["p"]) for _, p in points]],
                   capsize=3, color="#B3412F")
            ax.set_xticks(list(xs), [label for label, _ in points], fontsize=8, rotation=60, ha="right")
            ax.set_ylim(0, 1.05)
            ax.set_ylabel("Attack success (95% Wilson interval)")
            ax.set_title("Red team: attack success with guardrails on and with each one off")
            fig.tight_layout()
            path = out_dir / "redteam_ablation.png"
            fig.savefig(path)
            plt.close(fig)
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate every table and chart in eval/REPORT.md from stored result files.")
    parser.add_argument("--results-root", type=Path, default=RESULTS_ROOT)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--charts-dir", type=Path, default=CHARTS_DIR)
    args = parser.parse_args(argv)
    try:
        runs = load_runs(args.results_root)
    except results_io.ProvenanceError as exc:
        print(f"Refusing to report: {exc}", file=sys.stderr)
        return 1
    tables = {
        "headline-fresh": render_agent_table(runs, "fresh"),
        "baselines-fresh": render_baseline_table(runs, "fresh"),
        "headline-heldout": render_agent_table(runs, "heldout"),
        "dev": render_agent_table(runs, "dev"),
        "baselines": render_baseline_table(runs),
        "redteam": render_redteam_table(runs),
        "benign": render_benign_table(runs),
        "provenance": render_provenance_table(runs),
        "calls": render_calls(args.results_root),
    }
    text = update_markers(args.report.read_text(encoding="utf-8"), tables)
    args.report.write_text(text, encoding="utf-8", newline="\n")
    charts = write_charts(runs, args.charts_dir)
    print(f"Updated {args.report} from {len(runs)} result file(s); wrote {len(charts)} chart(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
