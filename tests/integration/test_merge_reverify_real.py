"""Real-environment check for the detached merge-gate re-verify job.

Unlike the unit tests, nothing here is mocked: a real child process is started
with ``-m pipeline.merge_reverify``, it runs a real pytest suite in a real git
worktree, and ``collect_merge_reverify`` polls until the result lands.  The
point is to prove the end-to-end wiring (child ``cwd`` = the control plane's
source root, the manifest read path, the atomic result write) works against a
live interpreter, not just against patched seams.

Every case is bounded by a deadline and fails loudly instead of hanging.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle.
import pipeline.server  # noqa: F401
import pipeline.merge_reverify as mr
# isort: on

DEADLINE_S = 120.0


def _git(worktree: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "pipeline test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "pipeline test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


def _make_worktree(root: Path, body: str) -> Path:
    """Create a git repo holding one pytest test file named ``test_sample.py``.

    ``.fagan.json`` pins the test command to this interpreter, so the gate runs
    a real pytest suite against the worktree instead of the no-op fallback that
    ``detect_test_command`` returns for a repo with no build marker.
    """
    worktree = root / "wt"
    worktree.mkdir()
    (worktree / "test_sample.py").write_text(body)
    (worktree / ".fagan.json").write_text(
        json.dumps({"test_cmd": [sys.executable, "-m", "pytest", "-q"]})
    )
    _git(worktree, "init", "-q")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "initial sample suite")
    return worktree


def _write_manifest(plan_dir: Path, plan_name: str, story_key: str, worktree: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "stories": {
            story_key: {
                "agent_instructions": "GOAL: keep the sample suite green",
                "worktree": str(worktree),
                "last_test_check": "2026-10-01T00:00:00Z",
            }
        }
    }
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _poll(job: dict) -> dict:
    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline:
        result = mr.collect_merge_reverify(job, DEADLINE_S)
        if result is not None:
            return result
        time.sleep(0.2)
    pytest.fail(f"merge re-verify job did not finish within {DEADLINE_S:g}s")


def test_real_job_reports_pass_then_fail(tmp_path, monkeypatch):
    plan_name = "REALPLAN"
    story_key = "REAL-1"
    plan_dir = tmp_path / "plans"
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    monkeypatch.setenv("PLAN_DIR", str(plan_dir))
    # An operator (or a container) may globally opt out of the full-suite
    # re-verify; this check exists to prove a real suite runs, so clear the
    # opt-out for the child process, which inherits this environment.
    monkeypatch.delenv("PIPELINE_REVERIFY_FULL_SUITE", raising=False)

    worktree = _make_worktree(
        tmp_path,
        "def test_one_plus_one():\n    assert 1 + 1 == 2\n",
    )
    manifest = _write_manifest(plan_dir, plan_name, story_key, worktree)
    manifest_before = manifest.read_bytes()
    state_root = tmp_path / "state"

    job = mr.start_merge_reverify(plan_name, story_key, str(worktree), state_root)
    try:
        passed = _poll(job)
    finally:
        mr.cancel_merge_reverify(job)
    print("\n[pass case] result:", json.dumps(passed, indent=2, sort_keys=True))
    print("[pass case] job.log tail:", Path(job["log_path"]).read_text()[-2000:])
    assert passed["state"] == "pass", passed
    assert passed["error"] == ""
    assert passed["sha"]
    assert "last_test_check" in passed

    # Second round: make the worktree suite red and expect a genuine "fail"
    # (never a harness "error").
    (worktree / "test_sample.py").write_text(
        "def test_one_plus_one():\n    assert 1 + 1 == 3\n"
    )
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "break it")
    job = mr.start_merge_reverify(plan_name, story_key, str(worktree), state_root)
    try:
        failed = _poll(job)
    finally:
        mr.cancel_merge_reverify(job)
    print("\n[fail case] result:", json.dumps(failed, indent=2, sort_keys=True))
    print("[fail case] job.log tail:", Path(job["log_path"]).read_text()[-2000:])
    assert failed["state"] == "fail", failed
    assert failed["error"]
    assert failed["sha"]
    assert failed["sha"] != passed["sha"]

    # Deterministic stale-bytecode guard.  Round 1 and round 2 differ inside one
    # line ("== 2" vs "== 3"), i.e. the two revisions have the SAME byte length,
    # and a rebase can land them inside the same wall-clock second.  CPython
    # validates a .pyc on (mtime truncated to seconds, size), so a cache taken
    # from the worktree's own ``__pycache__`` would satisfy both and the child
    # would grade round 1's compiled test again: either reporting "pass" for
    # this red suite or, at best, quoting round 1's green output.  Assert this
    # verdict's evidence is round 2's RED suite, so a regression that lets the
    # child grade stale bytecode fails deterministically instead of in roughly
    # one run out of three.
    evidence = failed["error"]
    assert "1 failed" in evidence, evidence
    assert "1 passed" not in evidence, evidence
    assert "test_one_plus_one" in evidence, evidence
    # And the child must not have populated the graded worktree's own cache at
    # all - the point of the per-job prefix is that the worktree's bytecode can
    # never be consulted again.
    assert not (worktree / "__pycache__").exists()

    # The job is read-only with respect to the manifest: only TP-04's caller
    # copies the reported last_test_check back under the plan lock.
    assert manifest.read_bytes() == manifest_before
