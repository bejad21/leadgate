"""Results files: one JSON line per (case, repeat), after a header line that says where the numbers came from.

    {"_header": {model_configured, run_date, git_sha, git_dirty, prompt_version, dataset, sampling, ...}}
    {"case_id": ..., "repeat": 0, "models": [...per LLM call], "tokens": {...}, "cost": ..., ...}

Which model produced a number is in the records (the model that answered each call, from the turn trace),
so a run that failed over mid-way shows it. A file with no header, or with a header missing a required
field, is rejected by every reader: a number with no provenance is not published.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
REQUIRED_HEADER_KEYS = (
    "schema_version", "kind", "domain", "model_configured", "run_date", "git_sha", "git_dirty", "prompt_version",
    "dataset", "sampling", "reference_date", "repeats", "command",
)
# Header fields that must be the same for a run to be resumed: the numbers would not be comparable otherwise.
RESUME_MUST_MATCH = ("kind", "domain", "prompt_version", "dataset", "sampling", "model_configured")


class ProvenanceError(Exception):
    """A results file that cannot say where its numbers came from."""


def git_state(root: Path | None = None) -> tuple[str, bool]:
    """(commit sha, working tree has uncommitted changes). ("unknown", False) outside a repository."""
    cwd = str(root or Path(__file__).resolve().parent.parent)
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False
    return sha, bool(status.strip())


def build_header(
    *, kind: str, domain: str, dataset_name: str, dataset_sha256: str, repeats: int, reference_date: str,
    sampling: dict, prompt_version: str, models_configured: list[str], argv: list[str], run_date: str,
    git_sha: str, git_dirty: bool, **extra,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "domain": domain,
        "model_configured": list(models_configured),
        "run_date": run_date,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "prompt_version": prompt_version,
        "dataset": {"name": dataset_name, "sha256": dataset_sha256},
        "sampling": dict(sampling),
        "reference_date": reference_date,
        "repeats": repeats,
        "command": " ".join(shlex.quote(a) if " " in a else a for a in argv),
        **extra,
    }


def start_run(path: Path, header: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_header": header}) + "\n", encoding="utf-8")


def append_record(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


@dataclass
class Run:
    header: dict
    records: list[dict]
    footer: dict | None = None


def _check_header(header: object, path: Path) -> dict:
    if not isinstance(header, dict):
        raise ProvenanceError(f"{path}: no header line, so the file cannot say which model, code or data produced it")
    for key in REQUIRED_HEADER_KEYS:
        if key not in header:
            raise ProvenanceError(f"{path}: the header is missing {key}")
    return header


def read_run(path: Path) -> Run:
    """The header and the latest record for each (case, repeat). Raises ProvenanceError without a valid header."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        first = json.loads(lines[0]) if lines else None
    except ValueError:
        first = None
    header = _check_header(first.get("_header") if isinstance(first, dict) else None, path)
    latest: dict[tuple, dict] = {}
    footer = None
    for line in lines[1:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line from an interrupted write
        if not isinstance(record, dict):
            continue
        if "_footer" in record:
            footer = record["_footer"]
            continue
        latest[(record.get("case_id"), record.get("repeat", 0))] = record
    return Run(header, list(latest.values()), footer)


def finished_keys(path: Path) -> set[tuple]:
    """(case_id, repeat) pairs that have a record without an error; a restart redoes the rest."""
    return {(r["case_id"], r.get("repeat", 0)) for r in read_run(path).records if not (r.get("actual") or {}).get("error")}


def check_resumable(path: Path, header: dict) -> None:
    existing = read_run(path).header
    for key in RESUME_MUST_MATCH:
        if existing.get(key) != header.get(key):
            raise ProvenanceError(
                f"{path} was started with a different {key} ({existing.get(key)!r} vs {header.get(key)!r}); "
                "use a new results directory"
            )


def write_footer(path: Path, footer: dict) -> None:
    append_record(path, {"_footer": footer})


def model_share(records: list[dict]) -> dict[str, float]:
    """Share of model calls answered by each model, from the per-call models in the records."""
    counts = Counter(m for r in records for m in r.get("models") or [])
    total = sum(counts.values())
    return {m: n / total for m, n in sorted(counts.items())} if total else {}


def is_mixed(records: list[dict]) -> bool:
    return len(model_share(records)) > 1


def totals(records: list[dict]) -> dict:
    return {
        "prompt_tokens": sum((r.get("tokens") or {}).get("prompt", 0) for r in records),
        "completion_tokens": sum((r.get("tokens") or {}).get("completion", 0) for r in records),
        "cost": sum(r.get("cost", 0.0) for r in records),
        "llm_calls": sum(len(r.get("models") or []) for r in records),
    }
