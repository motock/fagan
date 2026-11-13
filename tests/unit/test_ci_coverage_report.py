"""Guards for the non-gating coverage report in ci.yml's `test` job.

The report is a convenience artifact: it must be produced on exactly one matrix
leg, uploaded, and must never fail the job on a coverage number. PyYAML is not
a dependency of this repo, so ci.yml is read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

SHA = re.compile(r"[0-9a-f]{40}")
LEG = "matrix.os == 'ubuntu-latest' && matrix.python-version == '3.14'"
COV_ARGS = "--cov=pipeline --cov=app --cov=scripts --cov-report=xml:coverage.xml"


def _text() -> str:
    return CI.read_text(encoding="utf-8")


def _gate_flags(text: str) -> list[str]:
    return [flag for flag in ("--cov-fail-under", "fail_under") if flag in text]


def test_coverage_is_produced_on_exactly_one_leg() -> None:
    text = _text()
    assert text.count("EXTRA_PYTEST_ARGS:") == 1, (
        "the coverage flags must be attached to exactly one matrix leg"
    )
    assert LEG in text, "the coverage leg must be ubuntu-latest on Python 3.14"
    assert COV_ARGS in text, "the coverage leg must write coverage.xml"


def test_the_report_is_uploaded() -> None:
    text = _text()
    assert "name: coverage-xml" in text
    assert "path: coverage.xml" in text
    assert "if-no-files-found: error" in text, (
        "a run that produces no coverage.xml must fail loudly, not upload nothing"
    )


def test_the_upload_action_is_pinned_to_a_full_sha() -> None:
    match = re.search(r"uses:\s*actions/upload-artifact@(\S+)", _text())
    assert match is not None, "ci.yml no longer uploads the coverage artifact"
    assert SHA.fullmatch(match.group(1)) is not None, (
        f"the upload action must be pinned to a 40-char SHA, found {match.group(1)!r}"
    )


def test_coverage_never_gates_a_merge() -> None:
    assert _gate_flags(_text()) == [], (
        "the coverage report must stay non-gating: no coverage threshold"
    )


def test_requirements_comment_records_the_ci_artifact() -> None:
    line = next(
        line
        for line in (ROOT / "requirements-dev.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("pytest-cov==")
    )
    assert "ci.yml" in line
    assert "nothing in the suite or CI depends on it" not in line


def test_gate_scanner_flags_a_threshold() -> None:
    assert _gate_flags("pytest --cov=pipeline --cov-fail-under=80") == [
        "--cov-fail-under"
    ]


def _is_gitignored(name: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", name],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), f"git check-ignore failed for {name!r}"
    return result.returncode == 0


def test_coverage_outputs_are_gitignored() -> None:
    # tests/conftest.py fails the run when it leaves NEW untracked entries in
    # the repo root. The coverage leg writes coverage.xml and the .coverage
    # data file there, so both must be ignored or that leg fails every run.
    for name in ("coverage.xml", ".coverage", ".coverage.runner1234"):
        assert _is_gitignored(name), f"{name} must be gitignored"


def test_unrelated_root_files_are_not_gitignored() -> None:
    assert not _is_gitignored("coverage_notes.txt"), (
        "the coverage ignore rules must not hide unrelated root files"
    )
