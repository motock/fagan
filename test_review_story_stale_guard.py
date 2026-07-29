"""Tests for the stale/duplicate-call guard in review_story.

review_story's only production caller (advance_pipeline) gates on
``story["status"] == "tests_passed"`` before invoking it. A stale or
duplicate call that arrives after the story has already moved past that
state must no-op instead of re-running the reviewer backend, opening a
PR, or mutating the manifest.

These tests assert the guard fires *before* the existing
last_reviewed_sha / worktree SHA-comparison guard (so no subprocess/git
call is made), before any reviewer-backend dispatch (so _run_reviewer is
never invoked), and before any manifest write (so the on-disk manifest is
untouched). They are written to fail until the implementation adds the
early-return guard described in the story.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_pipeline_mcp_server.py / test_review_story_same_sha.py
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
# Helpers
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


def _assert_no_side_effects(plan_dir, plan_name, story_key, expected_status,
                            manifest_before, monkeypatch):
    """Shared post-conditions for every skip case."""
    story = _read_story(plan_dir, plan_name, story_key)
    assert story["status"] == expected_status, (
        f"status must remain {expected_status!r}, got {story['status']!r}"
    )
    # The manifest on disk must be byte-for-byte unchanged: the skip path
    # must not call _atomic_write_json (nothing changed, so no write needed).
    assert _manifest_bytes(plan_dir, plan_name) == manifest_before, (
        "manifest was rewritten by the skip path - nothing changed on disk, "
        "so no write should occur"
    )


# ---------------------------------------------------------------------------
# Parametrized skip cases: every non-tests_passed state must no-op.
# ---------------------------------------------------------------------------

NON_REVIEWABLE_STATUSES = [
    "done",
    "pr_open",
    "parked",
    "failed",
    "changes_requested",
    "in_progress",
]


@pytest.mark.parametrize("status", NON_REVIEWABLE_STATUSES)
def test_review_story_skips_when_status_not_tests_passed(
    plan_dir, agents_dir, monkeypatch, status,
):
    """Criteria (1)-(3) + changes_requested boundary: a story in any state
    other than tests_passed must be skipped - reviewer not invoked, status
    unchanged, manifest not rewritten."""
    story = _make_story(plan_dir, status=status)
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    # If the guard is missing, review_story will call _run_reviewer. Record
    # the call and assert below that it never happened (review_story's own
    # `except Exception` swallows raised sentinels, so we observe via a
    # side-effect flag instead of relying on an exception propagating).
    reviewer_calls = []

    def _reviewer_sentinel(*a, **k):
        reviewer_calls.append((a, k))
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer_sentinel)

    # Likewise no PR should be opened.
    pr_calls = []

    def _open_pr_sentinel(*a, **k):
        pr_calls.append((a, k))
        return "https://gh/pr/should-not-open"

    monkeypatch.setattr(p, "_open_pr", _open_pr_sentinel)

    result = p.review_story("rv", "S1")

    # The reviewer backend and PR opener must never have been invoked.
    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked for status={status!r} "
        f"(stale/duplicate review should no-op); calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked for status={status!r}; calls={pr_calls!r}"
    )

    # Return shape: ok=True, status is the unchanged on-disk status, and a
    # skipped reason string is present.
    assert result["ok"] is True, f"expected ok=True skip, got {result!r}"
    assert result["status"] == status, (
        f"returned status must be the unchanged on-disk status {status!r}, "
        f"got {result.get('status')!r}"
    )
    assert "skipped" in result, (
        f"skip result must carry a 'skipped' reason string, got {result!r}"
    )
    assert isinstance(result["skipped"], str) and result["skipped"], (
        f"'skipped' must be a non-empty reason string, got {result.get('skipped')!r}"
    )

    _assert_no_side_effects(plan_dir, "rv", "S1", status, manifest_before, monkeypatch)


# ---------------------------------------------------------------------------
# Missing / None status boundary: must not crash, must skip safely.
# ---------------------------------------------------------------------------

def test_review_story_skips_when_status_missing(plan_dir, agents_dir, monkeypatch):
    """Boundary: a story with no 'status' field at all must not crash and
    must be treated as not-reviewable (skip)."""
    story = _make_story(plan_dir, status="tests_passed")
    del story["status"]  # remove the field entirely
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    # Must not raise.
    result = p.review_story("rv", "S1")

    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked when status is missing; calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked when status is missing; calls={pr_calls!r}"
    )
    assert result["ok"] is True
    assert result.get("skipped")
    # status field was absent on disk; returned status should reflect that
    # (None or absent) rather than a fabricated reviewable state.
    assert result.get("status") in (None, "tests_passed") or "status" not in result, (
        f"unexpected returned status for missing-field story: {result!r}"
    )

    # Manifest unchanged on disk.
    assert _manifest_bytes(plan_dir, "rv") == manifest_before


def test_review_story_skips_when_status_is_none(plan_dir, agents_dir, monkeypatch):
    """Boundary: a story whose 'status' is explicitly None must not crash
    and must skip safely (treated as not reviewable)."""
    story = _make_story(plan_dir, status="tests_passed")
    story["status"] = None
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    result = p.review_story("rv", "S1")

    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked when status is None; calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked when status is None; calls={pr_calls!r}"
    )
    assert result["ok"] is True
    assert result.get("skipped")
    assert result.get("status") is None, (
        f"returned status should be None, got {result.get('status')!r}"
    )
    assert _manifest_bytes(plan_dir, "rv") == manifest_before


# ---------------------------------------------------------------------------
# Guard ordering: the new guard must fire BEFORE the SHA-comparison guard,
# so no git subprocess runs even when last_reviewed_sha matches HEAD.
# ---------------------------------------------------------------------------

def test_review_story_skip_fires_before_sha_guard_no_subprocess(
    plan_dir, agents_dir, monkeypatch,
):
    """The new state guard must run before the existing last_reviewed_sha /
    worktree SHA-comparison guard. Even when last_reviewed_sha would equal
    HEAD (which would otherwise trigger the SHA guard's own subprocess.run
    git rev-parse), a non-tests_passed story must skip without any git
    subprocess call at all."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    story = _make_story(plan_dir, status="done", worktree=str(worktree),
                        last_reviewed_sha="abc123def456")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    subprocess_calls = []

    def _subprocess_spy(*args, **kwargs):
        subprocess_calls.append(args)
        # Return a plausible Result so review_story continues; we assert
        # below that this was never reached.
        class _R:
            stdout = "abc123def456\n"
            returncode = 0
        return _R()

    monkeypatch.setattr(p.subprocess, "run", _subprocess_spy)
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    result = p.review_story("rv", "S1")

    assert result["ok"] is True
    assert result["status"] == "done"
    assert result.get("skipped")
    assert subprocess_calls == [], (
        f"expected zero subprocess calls (state guard must fire before SHA "
        f"guard), got {subprocess_calls!r}"
    )
    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked; calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked; calls={pr_calls!r}"
    )
    assert _manifest_bytes(plan_dir, "rv") == manifest_before


# ---------------------------------------------------------------------------
# No manifest write on the skip path.
# ---------------------------------------------------------------------------

def test_review_story_skip_does_not_write_manifest(
    plan_dir, agents_dir, monkeypatch,
):
    """Criterion (5): the skip path must not call _atomic_write_json with a
    mutated manifest, since nothing changes on disk. Spy on the writer to
    confirm it is never called for a skip."""
    story = _make_story(plan_dir, status="pr_open")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)
    manifest_before = _manifest_bytes(plan_dir, "rv")

    write_calls = []
    real_write = p._atomic_write_json

    def _write_spy(path, obj):
        write_calls.append((str(path), obj))
        # Delegate to the real writer so that if the implementation
        # *does* (incorrectly) write, downstream reads still work - but
        # we assert below that it never happened.
        real_write(path, obj)

    monkeypatch.setattr(p, "_atomic_write_json", _write_spy)
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda *a, **k: reviewer_calls.append((a, k)) or "VERDICT: APPROVE")
    pr_calls = []
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: pr_calls.append((a, k)) or "https://gh/x")

    result = p.review_story("rv", "S1")

    assert reviewer_calls == [], (
        f"_run_reviewer must not be invoked on the skip path; calls={reviewer_calls!r}"
    )
    assert pr_calls == [], (
        f"_open_pr must not be invoked on the skip path; calls={pr_calls!r}"
    )
    assert result["ok"] is True
    assert result["status"] == "pr_open"
    assert result.get("skipped")
    assert write_calls == [], (
        f"_atomic_write_json must not be called on the skip path, got {write_calls!r}"
    )
    assert _manifest_bytes(plan_dir, "rv") == manifest_before


# ---------------------------------------------------------------------------
# Regression guard: tests_passed stories are completely unaffected.
# ---------------------------------------------------------------------------

def test_review_story_tests_passed_still_runs_reviewer(
    plan_dir, agents_dir, monkeypatch,
):
    """Criterion (4): a story in the tests_passed state must NOT be skipped
    by the new guard - the full existing behavior (reviewer dispatch, PR
    opening on APPROVE) must still run exactly as before."""
    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    reviewer_called = []

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None):
        reviewer_called.append(True)
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    result = p.review_story("rv", "S1")

    assert reviewer_called == [True], (
        "_run_reviewer MUST be invoked for a tests_passed story"
    )
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert result["pr_url"] == "https://gh/pr/1"
    on_disk = _read_story(plan_dir, "rv", "S1")
    assert on_disk["status"] == "pr_open"
    assert on_disk["pr_url"] == "https://gh/pr/1"


def test_review_story_tests_passed_request_changes_still_runs(
    plan_dir, agents_dir, monkeypatch,
):
    """Criterion (4) negative side: a tests_passed story getting
    REQUEST_CHANGES (with real findings) must still flow through the
    existing verdict handling - the new guard must not short-circuit it."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "rv", "S1", story)

    reviewer_output = ("The error path is untested.\nVERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("rv", "S1")
    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "rv", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 1