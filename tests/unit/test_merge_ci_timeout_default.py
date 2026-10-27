"""Regression guard for the merge-gate CI poll default (MERGE-CI-01).

``PIPELINE_MERGE_CI_TIMEOUT`` is read once, at import time, so each test below
observes a *fresh* import in a *fresh* child process rather than reloading the
module in-process (reload order and already-bound module references make the
in-process version flaky).  Every child env is built from its own
``dict(os.environ)`` copy so the removal never leaks into other tests.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_MD = REPO_ROOT / "REFERENCE.md"

_PRINT_TIMEOUT = "import pipeline.ci; print(pipeline.ci.PIPELINE_MERGE_CI_TIMEOUT)"


def _run_child(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUT],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_default_ci_timeout_is_900() -> None:
    env = dict(os.environ)
    env.pop("PIPELINE_MERGE_CI_TIMEOUT", None)
    result = _run_child(env)
    assert result.stdout.strip() == "900", (
        "PIPELINE_MERGE_CI_TIMEOUT must default to 900 seconds so a six-job "
        f"matrix (macOS worst case 5m39s) finishes before the merge gate gives "
        f"up; got {result.stdout.strip()!r} (stderr: {result.stderr.strip()!r})"
    )


def test_env_override_still_wins() -> None:
    env = dict(os.environ)
    env["PIPELINE_MERGE_CI_TIMEOUT"] = "120"
    result = _run_child(env)
    assert result.stdout.strip() == "120", (
        "An explicit PIPELINE_MERGE_CI_TIMEOUT must still override the 900 "
        f"default; got {result.stdout.strip()!r} "
        f"(stderr: {result.stderr.strip()!r})"
    )


def test_reference_md_documents_default_900() -> None:
    rows = [
        line
        for line in REFERENCE_MD.read_text(encoding="utf-8").splitlines()
        if "PIPELINE_MERGE_CI_TIMEOUT" in line
    ]
    assert rows and all("900" in row for row in rows), (
        "REFERENCE.md must document the PIPELINE_MERGE_CI_TIMEOUT row with "
        f"default 900; found {rows!r}"
    )
