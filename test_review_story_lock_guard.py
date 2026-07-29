"""Tests for the per-plan lock guard around review_story (Mode 29 fix).

review_story is the only story-mutating MCP tool in pipeline/server.py that
does NOT acquire ``_plan_lock``. Every sibling that reads-then-writes a
story's manifest entry (patch_story, set_story_status, dispatch_story,
interrupt_story, advance_pipeline/_advance_pipeline_locked) already does.
Without the lock, a manual/interactive ``review_story`` call (or two
overlapping scheduler processes) can run fully concurrently with a tick's
own internal review+merge sequence: both read the story while it still says
``tests_passed``, the tick's path wins and merges (status -> ``done``), and
the now-stale other call overwrites the manifest's status back to
``changes_requested`` for the already-merged story.

The fix is purely additive: wrap the ENTIRE body of review_story in
``with _plan_lock(plan_name) as acquired:``, mirroring patch_story's exact
pattern. If ``not acquired``, return
``{"ok": True, "skipped": "locked", "reason": "..."}``. ``_plan_lock`` is
reentrant per-thread, so ``_advance_pipeline_locked``'s existing internal
call to review_story (already holding the lock for its tick) keeps working.

These tests are written to fail until the implementation adds that lock
acquisition. They are fully standalone (own fixtures/helpers, no cross-file
imports of test code).
"""

import fcntl
import json
import os

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_review_story_stale_guard.py / test_pipeline_mcp_server.py
# so this file is fully standalone).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers (replicated from test_review_story_stale_guard.py - do not import
# across test files).
# ---------------------------------------------------------------------------

def _write_manifest_with_story(plan_dir, plan_name, story_key, story):
    """Write a manifest containing a single story with arbitrary fields."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {story_key: story},
    }))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _manifest_bytes(plan_dir, plan_name):
    return (plan_dir / f"{plan_name}.manifest.json").read_bytes()


def _make_story(plan_dir, *, status, worktree=None, **extra):
    """Build a minimal story dict in the given state."""
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


def _approve_reviewer_and_pr(monkeypatch):
    """Wire up _run_reviewer -> APPROVE and _open_pr -> a fake URL, returning
    (reviewer_calls, pr_calls) lists so callers can assert invocation."""
    reviewer_calls = []

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None):
        reviewer_calls.append(True)
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)

    pr_calls = []

    def _fake_open_pr(wt, key, story):
        pr_calls.append(True)
        return "https://gh/pr/1"

    monkeypatch.setattr(p, "_open_pr", _fake_open_pr)
    return reviewer_calls, pr_calls


# ---------------------------------------------------------------------------
# (1) review_story skips when the plan lock is already held externally.
# ---------------------------------------------------------------------------

def test_review_story_skips_when_lock_held(plan_dir, agents_dir, monkeypatch):
    """With the plan's lock file already held by another (simulated) process
    and a story at status="tests_passed", review_story must acquire
    _plan_lock, find it unavailable, and skip immediately: ok=True,
    skipped="locked", no reviewer dispatch, no PR open, and the on-disk
    manifest byte-for-byte unchanged."""
    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    # Simulate another process holding the per-plan flock, exactly like
    # test_patch_story_skips_when_lock_held in test_pipeline_mcp_server.py.
    lock_path = plan_dir / "rv.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.review_story("rv", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True, f"expected ok=True skip, got {result!r}"
    assert result.get("skipped") == "locked", (
        f"expected skipped=='locked', got {result!r}"
    )
    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked when the lock is held; "
        f"calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked when the lock is held; calls={pr_calls!r}"
    )
    assert _manifest_bytes(plan_dir, "rv") == manifest_before, (
        "manifest must be byte-for-byte unchanged when the lock is held"
    )


# ---------------------------------------------------------------------------
# (2) review_story proceeds normally when the lock is free.
# ---------------------------------------------------------------------------

def test_review_story_proceeds_normally_when_lock_is_free(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression check: wrapping review_story in the lock must not change
    ordinary behavior. With a tests_passed story and a free lock, the
    reviewer IS invoked, an APPROVE verdict flows through to status ==
    'pr_open', and a PR is opened - exactly as the existing (untouched)
    test_review_story_tests_passed_still_runs_reviewer verifies."""
    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    reviewer_calls, pr_calls = _approve_reviewer_and_pr(monkeypatch)

    result = p.review_story("rv", "S1")

    assert reviewer_calls == [True], (
        "_run_reviewer MUST be invoked for a tests_passed story with a free lock"
    )
    assert pr_calls == [True], (
        "_open_pr MUST be invoked on an APPROVE verdict with a free lock"
    )
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert result["pr_url"] == "https://gh/pr/1"
    on_disk = _read_story(plan_dir, "rv", "S1")
    assert on_disk["status"] == "pr_open"
    assert on_disk["pr_url"] == "https://gh/pr/1"


# ---------------------------------------------------------------------------
# (3) review_story releases the lock after the call (even on the early-exit
#     skip path).
# ---------------------------------------------------------------------------

def test_review_story_lock_released_after_call(plan_dir, agents_dir, monkeypatch):
    """After review_story returns (here via the fast early-exit skip on a
    'todo' story), the per-plan lock must be free: a fresh non-blocking
    exclusive flock on the same <plan_name>.lock path must succeed without
    BlockingIOError. This proves the lock was released by the `with`
    statement rather than leaked."""
    story = _make_story(plan_dir, status="todo")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    # Sentinel reviewers must not be invoked (todo hits the early skip).
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    result = p.review_story("rv", "S1")

    # It should have skipped (not reviewable state) - the point of this test
    # is the lock release, but confirm it didn't run the reviewer.
    assert reviewer_calls == []
    assert pr_calls == []
    assert result["ok"] is True

    # Now the lock must be free: take a non-blocking exclusive flock ourselves.
    lock_path = plan_dir / "rv.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        # If review_story leaked the lock, this raises BlockingIOError.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pytest.fail(
            "review_story did not release _plan_lock after its early-exit "
            "skip path - a subsequent non-blocking acquire failed"
        )
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# (4) Reentrancy: review_story called from inside an already-held _plan_lock
#     (simulating _advance_pipeline_locked's internal call) still works.
# ---------------------------------------------------------------------------

def test_review_story_internal_call_from_advance_pipeline_still_works(
    plan_dir, agents_dir, monkeypatch,
):
    """The whole point of the fix is that _advance_pipeline_locked's existing
    internal call to review_story (made while it ALREADY holds _plan_lock for
    its tick) must keep working unchanged. _plan_lock is reentrant per-thread,
    so a nested acquisition inside the same thread yields True instantly
    rather than deadlocking or skipping as 'locked'.

    Rather than spin up the full advance_pipeline machinery, directly
    simulate the reentrant-call shape: acquire _plan_lock(plan_name) using
    the real context manager, and INSIDE that block call review_story on a
    tests_passed story. It must behave exactly like the free-lock case:
    reviewer invoked, APPROVE flows through, PR opened, nothing skipped."""
    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    reviewer_calls, pr_calls = _approve_reviewer_and_pr(monkeypatch)

    with pcon._plan_lock("rv") as acquired:
        assert acquired is True, (
            "the outer _plan_lock acquisition must succeed on a free lock"
        )
        # This nested call is the reentrant shape: review_story re-acquires
        # _plan_lock for the same plan in the same thread. It must NOT skip
        # as 'locked' - the per-thread held-set makes the nested acquire
        # instant and successful.
        result = p.review_story("rv", "S1")

    assert reviewer_calls == [True], (
        "_run_reviewer MUST be invoked on the nested/reentrant call - "
        "the lock must be reentrant, not skipped as 'locked'"
    )
    assert pr_calls == [True], (
        "_open_pr MUST be invoked on the nested/reentrant APPROVE path"
    )
    assert result.get("skipped") != "locked", (
        f"reentrant review_story must not skip as 'locked', got {result!r}"
    )
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert result["pr_url"] == "https://gh/pr/1"
    on_disk = _read_story(plan_dir, "rv", "S1")
    assert on_disk["status"] == "pr_open"
    assert on_disk["pr_url"] == "https://gh/pr/1"