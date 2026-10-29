"""Story-scoped manifest commit: review results must not clobber other stories.

``review_story`` reads the manifest once, runs the reviewer while the plan lock
is released, then used to persist the whole snapshot it read *before* the
review. Any change another actor made to a DIFFERENT story of the same plan in
that window (an ``agent_done`` wake, an operator ``set_story_status``) was
silently overwritten.

The fix is ``pipeline.story_commit.commit_story``: re-read the manifest under
the plan lock and replace only this story's record. The first test here is the
reproduction of the overwrite; the rest pin ``commit_story``'s contract
(replace-only, missing-story warning, lock timeout, thread reentrancy).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

# pipeline.server is imported explicitly and first: importing any pipeline
# submodule before it trips the pre-existing package import cycle.
import pipeline.server as p
from app.identity import LOCAL_OPERATOR
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import story_commit
from pipeline.actor import bind_principal

PLAN = "P1"

#: A subprocess that takes the plan's flock and holds it until killed. flock is
#: per open-file-description, so this contends with the parent process exactly
#: like a second MCP server would.
_HOLD_LOCK = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.stdout.write("locked\\n")
sys.stdout.flush()
time.sleep(float(sys.argv[2]))
"""

#: A subprocess that tries one non-blocking flock and reports it on stdout.
_TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stdout.write("busy\\n")
else:
    sys.stdout.write("free\\n")
"""


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # persistence and concurrency bind PLAN_DIR by value at their own module
    # load, so the patch has to land on those bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir: Path, manifest: dict) -> Path:
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _git_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "a@b.c"], cwd=worktree, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=worktree, check=True)
    (worktree / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=worktree, check=True, capture_output=True
    )
    return worktree


def _hold_lock_in_subprocess(lock_path: Path, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock_path), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked", "subprocess failed to take flock"
    return proc


# --------------------------------------------------------------------------
# Reproduction: the review window must not revert another story's update
# --------------------------------------------------------------------------


def test_review_story_keeps_other_story_update_made_during_review(
    plan_dir, tmp_path, monkeypatch
):
    """RED on the stale-snapshot write: S2's mid-review update must survive."""
    worktree = _git_worktree(tmp_path)
    manifest_path = _write_manifest(
        plan_dir,
        {
            "plan": {"name": PLAN},
            "wave": 3,
            "stories": {
                "S1": {
                    "worktree": str(worktree),
                    "status": "tests_passed",
                    "summary": "do the thing",
                },
                "S2": {"status": "agent_running"},
            },
        },
    )

    def fake_reviewer(*_args, **_kwargs):
        # A concurrent actor (agent_done wake / operator set_story_status)
        # updates a DIFFERENT story of the same plan while the review runs
        # with the plan lock released.
        updated = p._store.update_story(PLAN, "S2", {"status": "agent_done"})
        assert updated is not None, "mid-review update did not apply"
        on_disk = json.loads(manifest_path.read_text())
        assert on_disk["stories"]["S2"]["status"] == "agent_done", (
            "mid-review update never reached the manifest"
        )
        return "VERDICT: REQUEST_CHANGES\nsome finding"

    monkeypatch.setattr(p, "_run_reviewer", fake_reviewer)
    monkeypatch.setattr(p, "_notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_open_pr", lambda *_a, **_k: "https://example.invalid/pr/1")
    monkeypatch.setattr(p, "_post_pr_comment", lambda *_a, **_k: None)

    with bind_principal(LOCAL_OPERATOR):
        result = p.review_story(PLAN, "S1")

    assert result["ok"] is True
    final = json.loads(manifest_path.read_text())
    # Proves a write happened: the reviewed story moved off tests_passed.
    assert final["stories"]["S1"]["status"] == "changes_requested"
    # The bug: the stale snapshot wrote S2 back to agent_running.
    assert final["stories"]["S2"]["status"] == "agent_done"


# --------------------------------------------------------------------------
# commit_story contract
# --------------------------------------------------------------------------


def test_commit_story_replaces_only_the_named_story(plan_dir, monkeypatch):
    path = _write_manifest(
        plan_dir,
        {
            "plan": "P1",
            "created_at": "2026-10-01T00:00:00Z",
            "wave": 3,
            "stories": {
                "S1": {"status": "tests_passed", "branch": "b1"},
                "S2": {"status": "agent_running"},
            },
        },
    )
    writer = Mock()
    monkeypatch.setattr(p, "_atomic_write_json", writer)

    story = {"status": "reviewed", "branch": "b1"}
    story_commit.commit_story(PLAN, "S1", story, path)

    assert writer.call_count == 1
    written_path, written = writer.call_args[0]
    assert written_path == path
    assert written["plan"] == "P1"
    assert written["created_at"] == "2026-10-01T00:00:00Z"
    assert written["wave"] == 3
    assert written["stories"]["S1"] == story
    assert written["stories"]["S2"] == {"status": "agent_running"}


def test_commit_story_missing_story_warns_and_writes_nothing(
    plan_dir, monkeypatch, caplog
):
    path = _write_manifest(
        plan_dir,
        {
            "plan": "P1",
            "wave": 3,
            "stories": {"S1": {"status": "tests_passed"}, "S2": {"status": "todo"}},
        },
    )
    before = path.read_bytes()
    writer = Mock()
    monkeypatch.setattr(p, "_atomic_write_json", writer)

    with caplog.at_level(logging.WARNING):
        story_commit.commit_story(PLAN, "S9", {"status": "reviewed"}, path)

    assert writer.call_count == 0
    assert path.read_bytes() == before
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].plan == PLAN
    assert warnings[0].story == "S9"

    # A later commit of an existing story still works and does not resurrect
    # the dropped one (setdefault would).
    story_commit.commit_story(PLAN, "S1", {"status": "reviewed"}, path)
    assert writer.call_count == 1
    written = writer.call_args[0][1]
    assert written["stories"]["S2"] == {"status": "todo"}
    assert "S9" not in written["stories"]


def test_commit_story_raises_when_plan_lock_never_acquired(plan_dir, monkeypatch):
    path = _write_manifest(
        plan_dir,
        {"plan": "P1", "stories": {"S1": {"status": "tests_passed"}}},
    )
    before = path.read_bytes()
    monkeypatch.setattr(pcon, "_plan_reacquire_timeout", lambda: 0.3)
    lock_path = pcon.PLAN_DIR / f"{PLAN}.lock"
    holder = _hold_lock_in_subprocess(lock_path, 10)
    try:
        with pytest.raises(pcon.PlanLockReacquireTimeout):
            story_commit.commit_story(PLAN, "S1", {"status": "reviewed"}, path)
    finally:
        holder.kill()
        holder.wait(timeout=5)

    assert path.read_bytes() == before


def test_commit_story_is_reentrant_under_a_held_plan_lock(plan_dir, monkeypatch):
    path = _write_manifest(
        plan_dir,
        {
            "plan": "P1",
            "stories": {
                "S1": {"status": "tests_passed"},
                "S2": {"status": "tests_passed"},
            },
        },
    )
    lock_path = pcon.PLAN_DIR / f"{PLAN}.lock"
    errors: list[BaseException] = []

    def body() -> None:
        try:
            with pcon._plan_lock(PLAN) as acquired:
                assert acquired is True
                story_commit.commit_story(PLAN, "S1", {"status": "reviewed"}, path)
                # Reads the file again: must see the first commit's write.
                story_commit.commit_story(PLAN, "S2", {"status": "reviewed"}, path)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the parent
            errors.append(exc)

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "commit_story deadlocked under a held plan lock"
    assert errors == []

    written = json.loads(path.read_text())
    assert written["stories"]["S1"] == {"status": "reviewed"}
    assert written["stories"]["S2"] == {"status": "reviewed"}

    # The outer _plan_lock exit must have released the flock for real: a
    # refcount that leaked would leave it held and wedge the next transaction.
    probe = subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(lock_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert probe.stdout.strip() == "free"
