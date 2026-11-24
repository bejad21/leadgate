"""The CI workflow runs the same pinned images as docker-compose.yml and cannot hang for hours."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_the_odoo_job_uses_the_image_pinned_in_compose():
    pinned = re.search(r"image:\s*(odoo:\S+@sha256:[0-9a-f]{64})", COMPOSE)
    assert pinned, "docker-compose.yml no longer pins Odoo by digest"
    assert pinned.group(1) in CI
    assert not re.search(r"\bodoo:18\.0(?!@)", CI)


def test_the_postgres_service_uses_the_version_pinned_in_compose():
    pinned = re.search(r"image:\s*(postgres:\S+)", COMPOSE)
    assert pinned and f"image: {pinned.group(1)}" in CI


def test_every_job_has_a_timeout():
    jobs_section = CI.split("\njobs:\n", 1)[1]
    job_names = re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", jobs_section, flags=re.MULTILINE)
    assert len(job_names) == 3
    assert len(re.findall(r"^    timeout-minutes:\s*\d+\s*$", jobs_section, flags=re.MULTILINE)) == len(job_names)


def test_the_coverage_comment_matches_what_is_measured():
    assert "93.9%" not in CI
