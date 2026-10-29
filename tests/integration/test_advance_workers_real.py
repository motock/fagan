"""Real-environment check: pooled long-lived workers recycle plan latency.

No fake worker here. Two real ``pipeline.advance_worker`` children serve three
plans whose manifests are already all-done (so an advance is a cheap no-op
that still walks the real orchestration tick). Each plan is requested twice:

* the first round pays ``pipeline.server``'s import once per child;
* the second round must be served by the SAME two children, so its three
  advances cost only the advances themselves.

A pooled reconcile (``pipeline/advance_pool.py``) would pay a fresh import per
plan; that is the latency this pool removes. Run with ``-s`` to see the timings
and counters printed below.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401  -- import-cycle guard

PLANS = ("worker-pool-a", "worker-pool-b", "worker-pool-c")

#: The second round runs on two warm children; three no-op advances must fit.
_WARM_ROUND_SECONDS = 1.5


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
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


def _manifest(plan_name: str, repo: Path) -> dict:
    return {
        "plan_name": plan_name,
        "repo_root": str(repo),
        "stories": {
            "S-1": {
                "key": "S-1",
                "summary": "already finished",
                "status": "done",
                "dependencies": [],
                "acceptance": [],
                "agent_instructions": "do nothing",
                "persona": "author",
                "model": "gpt-oss:20b",
                "risk": "low",
            }
        },
    }


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three all-done plans in a scratch plan store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("scratch repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    for plan_name in PLANS:
        (plans / f"{plan_name}.manifest.json").write_text(
            json.dumps(_manifest(plan_name, repo), indent=2), encoding="utf-8"
        )

    monkeypatch.setenv("PLAN_DIR", str(plans))
    monkeypatch.setenv("WORKTREE_ROOT", str(worktrees))
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    monkeypatch.setenv("PIPELINE_AUTONOMY", "gated")
    return plans


def _wait_for_advances(pool, target: int, timeout_s: float) -> float:
    """Return seconds until ``pool`` reports *target* advances."""
    started = time.monotonic()
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        if pool.stats()["advances"] >= target:
            return time.monotonic() - started
        time.sleep(0.02)
    raise AssertionError(
        f"only {pool.stats()['advances']} of {target} advances in {timeout_s}s"
    )


def test_two_workers_serve_both_rounds_without_respawning(scratch: Path) -> None:
    from pipeline.advance_queue import AdvanceQueue
    from pipeline.advance_workers import AdvanceWorkerPool

    queue = AdvanceQueue()
    pool = AdvanceWorkerPool(size=2, queue=queue, plan_timeout_s=lambda: 120.0)
    overall_started = time.monotonic()
    pool.start()
    try:
        for plan_name in PLANS:
            assert queue.request(plan_name, reason="cold round")
        cold_seconds = _wait_for_advances(pool, 3, 180.0)

        for plan_name in PLANS:
            assert queue.request(plan_name, reason="warm round")
        warm_seconds = _wait_for_advances(pool, 6, 180.0)
        total_seconds = time.monotonic() - overall_started
        stats = pool.stats()
    finally:
        pool.stop(timeout_s=15.0)

    print(
        f"\nadvance workers real check: round1={cold_seconds:.2f}s "
        f"round2={warm_seconds:.2f}s total={total_seconds:.2f}s stats={stats}",
        flush=True,
    )

    assert stats["spawns"] == 2
    assert stats["timeouts"] == 0
    assert stats["advances"] == 6
    assert warm_seconds < _WARM_ROUND_SECONDS


