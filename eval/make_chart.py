"""Render eval/charts/final_metrics_by_domain.png from the saved eval results.

Usage: python -m eval.make_chart
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import metrics

DOMAINS = {"Cars": "cars", "Real estate": "real_estate"}
LABELS = ["Tool selection\naccuracy", "Slot extraction\naccuracy", "Grounding rate\n(1 - hallucination)", "Task completion\nrate"]
COLORS = {"Cars": "#2A78D6", "Real estate": "#EC6A36"}


def main() -> None:
    scores = {}
    counts = {}
    for name, key in DOMAINS.items():
        results = metrics.load_results(ROOT / "eval" / "results" / f"{key}_results.json")
        counts[name] = len(results)
        scores[name] = [
            metrics.tool_selection_accuracy(results),
            metrics.slot_extraction_accuracy(results),
            1 - metrics.hallucination_rate(results),
            metrics.task_completion_rate(results),
        ]

    fig, ax = plt.subplots(figsize=(11, 6.6), dpi=160)
    fig.patch.set_facecolor("#FBFBF9")
    ax.set_facecolor("#FBFBF9")
    width = 0.34
    for offset, (name, values) in zip((-width / 2, width / 2), scores.items()):
        xs = [i + offset for i in range(len(LABELS))]
        bars = ax.bar(xs, values, width, color=COLORS[name], label=f"{name} (n={counts[name]})")
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.012, f"{value:.2%}", ha="center", fontsize=11)

    ax.set_xticks(range(len(LABELS)))
    ax.set_xticklabels(LABELS, fontsize=11, color="#444")
    ax.set_ylim(0, 1.12)
    ax.set_yticks([i / 5 for i in range(6)])
    ax.set_yticklabels([f"{i * 20}%" for i in range(6)], color="#555")
    ax.set_ylabel("Score", color="#555")
    ax.yaxis.grid(True, color="#DDDDDA")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_title(f"LeadGate eval results, final run ({sum(counts.values())} cases total)", loc="left", fontsize=15)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, frameon=False, fontsize=11)
    fig.tight_layout()
    out = ROOT / "eval" / "charts" / "final_metrics_by_domain.png"
    fig.savefig(out, facecolor=fig.get_facecolor())
    print("wrote", out)
    for name, values in scores.items():
        print(name, [f"{v:.4f}" for v in values])


if __name__ == "__main__":
    main()
