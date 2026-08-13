"""Tests for the ``ci_pending_stalled`` warning emitted on the expired-pending
merge-gate branch.

Background: when a story's CI has been ``pending`` longer than the merge-gate
patience bound (``MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT``), the
non-blocking merge gate in ``_advance_pipeline_locked`` gives up the wait. It
previously cleared ``ci_pending_since`` / ``ci_pending_sha`` silently and fell
through to the ordinary ``gate_error`` accounting path. This story ADDS a
single structured ``_notify_user`` call - severity ``warning``, event
``ci_pending_stalled`` - fired exactly once per pending episode, BEFORE the
pops (because the message interpolates ``story['ci_pending_since']``, which
the very next line deletes). The pre-existing free-text merge-gate
notification ("merge gate attempt" / "merge gate failed") is unchanged and
must still fire alongside it.

These tests deliberately do NOT patch ``_notify_user``: the real
``pipeline.persistence._notify_user`` must run so it writes the JSONL sidecar
(``<plan>.notifications.jsonl``) and the free-text log
(``<plan>.notifications.log``) into the isolated ``plan_dir``. Every other
external boundary the merge adjudication phase touches is stubbed. They are
written against the *target* behaviour and fail (no ``ci_pending_stalled``
record is written) until ``pipeline/server.py`` inserts the ``_notify_user``
call on the expired-pending branch.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.server as p
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


def _expired_since():
    """An ISO timestamp old enough that ``_ci_pending_expired`` is True."""
    bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
    return (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()


def _fresh_since():
    """An ISO timestamp fresh enough that ``_ci_pending_expired`` is False."""
    return datetime.now(timezone.utc).isoformat()


def _patch_merge_boundaries(monkeypatch, ci_once, *, rebase_push=None):
    """Stub every external boundary the merge adjudication phase touches EXCEPT
    ``_notify_user``.

    ``_notify_user`` is intentionally left as the real implementation so its
    JSONL sidecar and free-text log writes land in the isolated ``plan_dir`` -
    that is the behaviour under test here. ``ci_once`` is the fake
    ``_ci_status_once`` to install. ``rebase_push`` is an optional fake
    ``_rebase_and_push_for_merge`` returning ``(gate_error, pushed_sha)``; when
    omitted, a default that reports a successful push of a fixed SHA is
    installed.
    """
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_ci_status_once", ci_once)
    # The blocking variant must NEVER be called by the tick.
    monkeypatch.setattr(
        p, "_ci_status",
        lambda *a, **k: pytest.fail("_ci_status (blocking) called by tick"),
    )
    if rebase_push is None:
        def _default_rp(plan_name, key, branch, worktree):
            return ("", "deadbeefcafe")
        rebase_push = _default_rp
    monkeypatch.setattr(p, "_rebase_and_push_for_merge", rebase_push)
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda story, worktree, key: {"state": "pass"},
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda worktree: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(
        p, "_mark_plane_done", lambda *a, **k: None,
    )


def _read_notification_records(plan_dir, plan_name):
    """Read every JSONL record from ``<plan>.notifications.jsonl`` (may be
    empty if the file does not exist)."""
    path = plan_dir / f"{plan_name}.notifications.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _stalled_records(plan_dir, plan_name):
    return [
        r for r in _read_notification_records(plan_dir, plan_name)
        if r.get("event") == "ci_pending_stalled"
    ]


def _read_notification_log(plan_dir, plan_name):
    """Read the free-text ``<plan>.notifications.log`` (may be empty)."""
    path = plan_dir / f"{plan_name}.notifications.log"
    if not path.exists():
        return ""
    return path.read_text()


# ---------------------------------------------------------------------------
# Expired-pending path emits the structured ci_pending_stalled warning.
# ---------------------------------------------------------------------------
class TestExpiredPendingWritesWarningRecord:
    def test_expired_pending_writes_warning_record(self, plan_dir, monkeypatch):
        key = "P1"
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")

        records = _stalled_records(plan_dir, "go")
        assert len(records) == 1, f"expected exactly one ci_pending_stalled record, got {records}"
        rec = records[0]
        assert rec["severity"] == "warning"
        assert rec["event"] == "ci_pending_stalled"
        assert rec["story_key"] == key
        assert rec["dedup_key"] == f"ci_pending_stalled:{key}"

    def test_message_names_the_story(self, plan_dir, monkeypatch):
        key = "P1"
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")

        records = _stalled_records(plan_dir, "go")
        assert len(records) == 1
        message = records[0]["message"]
        assert key in message, f"story key {key!r} not in message {message!r}"
        # The message must also name the pending-since timestamp it
        # interpolates - proving the call fired BEFORE the pop that deletes it.
        assert old in message, (
            f"ci_pending_since value {old!r} not in message {message!r}; "
            "the notify must fire before the pop deletes ci_pending_since"
        )

    def test_record_plan_field_matches(self, plan_dir, monkeypatch):
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")

        records = _stalled_records(plan_dir, "go")
        assert len(records) == 1
        assert records[0]["plan"] == "go"


# ---------------------------------------------------------------------------
# Still-pending (not yet past the bound) must stay silent.
# ---------------------------------------------------------------------------
class TestStillPendingEmitsNoStalledRecord:
    def test_still_pending_emits_no_stalled_record(self, plan_dir, monkeypatch):
        fresh = _fresh_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=fresh,
            )},
        )

        result = _advance_pipeline_locked("go")

        assert _stalled_records(plan_dir, "go") == []
        # The story is still pending, so it must still be appended to the
        # summary's ci_pending list (the else: ci_wait = True branch).
        assert "ci_pending" in result
        assert "P1" in result["ci_pending"]

    def test_still_pending_preserves_pending_fields(self, plan_dir, monkeypatch):
        # A still-pending story must NOT have its pending fields cleared -
        # only the expired branch pops them.
        fresh = _fresh_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=fresh,
            )},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story.get("ci_pending_since") == fresh
        assert story.get("ci_pending_sha") == "recordedsha"


# ---------------------------------------------------------------------------
# A passing CI result writes no ci_pending_stalled record.
# ---------------------------------------------------------------------------
class TestPassingCiEmitsNoStalledRecord:
    def test_passing_ci_emits_no_stalled_record(self, plan_dir, monkeypatch):
        fresh = _fresh_since()

        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=fresh,
            )},
        )

        _advance_pipeline_locked("go")

        assert _stalled_records(plan_dir, "go") == []
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["status"] == "done"


# ---------------------------------------------------------------------------
# The pre-existing free-text merge-gate notification still fires on the
# expired path, alongside the new structured record.
# ---------------------------------------------------------------------------
class TestExistingGenericGateNotificationStillEmitted:
    def test_generic_gate_notification_still_in_log(self, plan_dir, monkeypatch):
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        # merge_attempts defaults to 0, so the expired path falls through to
        # the "merge gate attempt 1/N ... will retry" branch (attempts < cap).
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")

        log = _read_notification_log(plan_dir, "go")
        assert log, "expected the free-text notifications.log to be non-empty"
        # The pre-existing generic merge-gate notification must still appear.
        assert ("merge gate attempt" in log) or ("merge gate failed" in log), (
            f"expected a generic 'merge gate attempt'/'merge gate failed' "
            f"line in notifications.log, got:\n{log}"
        )
        # And the new structured record must also be present.
        assert len(_stalled_records(plan_dir, "go")) == 1

    def test_generic_and_stalled_both_present_at_max(self, plan_dir, monkeypatch):
        # At the cap, the expired path terminal-fails and emits the
        # "merge gate failed" generic notification; the stalled record must
        # still be present too.
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                merge_attempts=MERGE_MAX_ATTEMPTS - 1,
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")

        log = _read_notification_log(plan_dir, "go")
        assert "merge gate failed" in log, (
            f"expected 'merge gate failed' in notifications.log, got:\n{log}"
        )
        assert len(_stalled_records(plan_dir, "go")) == 1


# ---------------------------------------------------------------------------
# The insert must not have changed the pops: on the expired path both pending
# fields are still cleared from the persisted story.
# ---------------------------------------------------------------------------
class TestPendingFieldsAreStillCleared:
    def test_pending_fields_are_still_cleared(self, plan_dir, monkeypatch):
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert "ci_pending_since" not in story, (
            f"ci_pending_since must be popped on the expired path, got {story}"
        )
        assert "ci_pending_sha" not in story, (
            f"ci_pending_sha must be popped on the expired path, got {story}"
        )

    def test_expired_still_counts_merge_attempt(self, plan_dir, monkeypatch):
        # Regression guard: the insert must not disturb the downstream
        # merge_attempts accounting on the expired path.
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                merge_attempts=1,
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["merge_attempts"] == 2


# ---------------------------------------------------------------------------
# Fires exactly once per episode: a second tick on the same (now-cleared)
# story does not re-emit the stalled record, because the expired branch pops
# the pending fields and ends the episode.
# ---------------------------------------------------------------------------
class TestFiresOncePerEpisode:
    def test_second_tick_does_not_duplicate_stalled_record(
        self, plan_dir, monkeypatch
    ):
        old = _expired_since()

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")
        first_count = len(_stalled_records(plan_dir, "go"))
        assert first_count == 1

        # Second tick: ci_pending_since was popped, so the story re-enters the
        # pending branch fresh (setdefault re-stamps it) and is NOT expired
        # on this tick -> no second stalled record.
        _advance_pipeline_locked("go")
        assert len(_stalled_records(plan_dir, "go")) == 1, (
            "the stalled record must not be re-emitted on a second tick for "
            "the same episode"
        )