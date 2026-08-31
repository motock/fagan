"""Tests for non-blocking pending-CI handling in the merge adjudication phase.

The merge gate in ``_advance_pipeline_locked`` previously called the blocking
``_ci_status`` (which sleeps in a loop up to ``PIPELINE_MERGE_CI_TIMEOUT``
seconds).  This story replaces those two call sites with the single-poll
``_ci_status_once`` so a *pending* CI result yields the tick instead of
blocking every other plan.  A pending poll that neither counts an attempt nor
has a deadline is an unbounded silent wait, so the old
``MERGE_MAX_ATTEMPTS`` bound is preserved in a *time-based* form via
``_ci_pending_expired``.

These tests mock ``_ci_status_once`` (and the other merge-gate boundaries) and
never call ``gh``.  They are written against the *target* behaviour and fail
(import/attribute errors) until ``pipeline/server.py`` implements the change.
"""

import contextlib
import inspect
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.server as p
from pipeline import merge
from pipeline.server import (
    MERGE_MAX_ATTEMPTS,
    PIPELINE_MERGE_CI_TIMEOUT,
    _advance_pipeline_locked,
)


# ---------------------------------------------------------------------------
# Manifest helpers (local copies so this file is self-contained).
# ---------------------------------------------------------------------------
def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _story(**overrides):
    base = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": "/nonexistent-worktree",
    }
    base.update(overrides)
    return base


def _patch_merge_boundaries(monkeypatch, ci_once):
    """Stub every external boundary the merge adjudication phase touches.

    ``ci_once`` is the fake ``_ci_status_once`` to install.  Everything else is
    made to pass so the only variable under test is the CI poll result.
    """
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_ci_status_once", ci_once)
    # The blocking variant must NEVER be called by the tick after this change.
    monkeypatch.setattr(
        p, "_ci_status",
        lambda *a, **k: pytest.fail("_ci_status (blocking) called by tick"),
    )
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda story, worktree, key: {"state": "pass"},
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda worktree: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_mark_plane_done", lambda *a, **k: None,
    )


# ---------------------------------------------------------------------------
# _ci_pending_expired unit tests.
# ---------------------------------------------------------------------------
class TestCiPendingExpired:
    def test_returns_false_for_malformed_timestamp(self):
        # A malformed timestamp must never expire a story.
        assert p._ci_pending_expired("not-a-timestamp") is False

    def test_returns_false_for_empty_string(self):
        assert p._ci_pending_expired("") is False

    def test_returns_false_for_recent_timestamp(self):
        recent = datetime.now(timezone.utc).isoformat()
        assert p._ci_pending_expired(recent) is False

    def test_returns_true_when_older_than_bound(self):
        # Older than the total patience the blocking gate provided.
        bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
        old = (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()
        assert p._ci_pending_expired(old) is True

    def test_returns_false_for_none(self):
        # Missing timestamp must not raise.
        assert p._ci_pending_expired(None) is False


# ---------------------------------------------------------------------------
# Pending CI behaviour in the merge adjudication phase.
# ---------------------------------------------------------------------------
class TestPendingCi:
    def test_pending_leaves_status_pr_open(self, plan_dir, monkeypatch):
        calls = {"count": 0}

        def ci_once(branch, *, sha):
            calls["count"] += 1
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["status"] == "pr_open"

    def test_pending_does_not_increment_merge_attempts(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story(merge_attempts=2)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["merge_attempts"] == 2

    def test_pending_does_not_park(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert "parked_reason" not in story or story["parked_reason"] is None
        assert story["status"] != "parked"

    def test_pending_adds_key_to_summary_ci_pending(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        result = _advance_pipeline_locked("go")
        assert "ci_pending" in result
        assert "P1" in result["ci_pending"]

    def test_pending_sets_ci_pending_since_first_observation(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"].get("ci_pending_since") is not None

    def test_pending_does_not_overwrite_ci_pending_since(self, plan_dir, monkeypatch):
        first = datetime.now(timezone.utc).isoformat()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story(ci_pending_since=first)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        # The pre-existing timestamp must be preserved, not overwritten.
        assert man["stories"]["P1"]["ci_pending_since"] == first

    def test_subsequent_pass_clears_ci_pending_since(self, plan_dir, monkeypatch):
        first = datetime.now(timezone.utc).isoformat()

        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story(ci_pending_since=first)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert "ci_pending_since" not in man["stories"]["P1"]

    def test_expired_pending_increments_merge_attempts(self, plan_dir, monkeypatch):
        # A pending result whose ci_pending_since is older than the total
        # patience bound MUST still count against merge_attempts (the bound
        # still fires in time-based form).
        bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
        old = (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story(merge_attempts=1, ci_pending_since=old)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["merge_attempts"] == 2

    def test_expired_pending_terminal_fails_at_max(self, plan_dir, monkeypatch):
        # An expired-pending story terminal-fails at MERGE_MAX_ATTEMPTS exactly
        # like any other gate_error.
        bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
        old = (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(merge_attempts=MERGE_MAX_ATTEMPTS - 1, ci_pending_since=old)},
        )

        result = _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story["status"] == "failed"
        assert "P1" in result.get("notify", []) or story.get("status") == "failed"

    def test_fail_still_counts_against_merge_attempts(self, plan_dir, monkeypatch):
        # Regression guard: a 'fail' result still counts exactly as before.
        def ci_once(branch, *, sha):
            return {"state": "fail", "error": "tests failed"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story(merge_attempts=1)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["merge_attempts"] == 2

    def test_summary_ci_pending_exists_empty_when_none_pending(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        result = _advance_pipeline_locked("go")
        assert "ci_pending" in result
        assert result["ci_pending"] == []

    def test_pending_does_not_merge(self, plan_dir, monkeypatch):
        merged = []

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        assert merged == []

    def test_pending_does_not_notify(self, plan_dir, monkeypatch):
        notified = []

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notified.append(a))
        _write_manifest(plan_dir, "go", {"P1": _story()})

        result = _advance_pipeline_locked("go")
        assert notified == []
        assert "P1" not in result.get("notify", [])


# ---------------------------------------------------------------------------
# Import / structural guards.
# ---------------------------------------------------------------------------
class TestStructure:
    def test_ci_status_once_imported(self):
        # _ci_status_once must be importable from pipeline.server.
        assert hasattr(p, "_ci_status_once")

    def test_ci_pending_expired_helper_exists(self):
        assert hasattr(p, "_ci_pending_expired")
        assert callable(p._ci_pending_expired)

    def test_ci_status_once_not_called_as_blocking_in_tick(self, plan_dir, monkeypatch):
        # The blocking _ci_status must not be invoked during a pending tick.
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})
        # If the implementation still calls _ci_status, the patched stub raises.
        _advance_pipeline_locked("go")