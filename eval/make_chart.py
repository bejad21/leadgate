"""Draw the eval charts from the stored result files.

    python -m eval.make_chart

The drawing code lives in eval.report (`write_charts`), so `python -m eval.report` and this command make
the same files: eval/charts/heldout_metrics.png and eval/charts/redteam_ablation.png.
"""
import sys

from eval import report


def main() -> int:
    try:
        runs = report.load_runs(report.RESULTS_ROOT)
    except report.results_io.ProvenanceError as exc:
        print(f"Refusing to draw: {exc}", file=sys.stderr)
        return 1
    for path in report.write_charts(runs, report.CHARTS_DIR):
        print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
