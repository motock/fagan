"""Tests for ``ci_pending_sha`` caching in the merge adjudication phase.

Background: with the non-blocking ``_ci_status_once`` poll, every later tick
on a pending story re-rebased + force-pushed unconditionally. Whenever the
default branch had moved that minted a new SHA, force-pushed it, and restarted
CI - burning Actions minutes and delaying convergence. This story records the
just-pushed SHA as ``story['ci_pending_sha']`` on a pending observation and,
on later ticks, skips the rebase/force-push entirely and polls that exact SHA
until CI resolves or ``_ci_pending_expired`` fires.

These tests mock ``_ci_status_once`` and ``_rebase_and_push_for_merge`` (and
the other merge-gate boundaries) and never call ``gh`` or ``git``. They are
written against the *target* behaviour and fail (import/attribute errors)
until ``pipeline/server.py`` implements the change.
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


def _patch_merge_boundaries(monkeypatch, ci_once, *, rebase_push=None):
    """Stub every external boundary the merge adjudication phase touches.

    ``ci_once`` is the fake ``_ci_status_once`` to install. ``rebase_push`` is
    an optional fake ``_rebase_and_push_for_merge`` returning
    ``(gate_error, pushed_sha)``; when omitted, a default that reports a
    successful push of a fixed SHA is installed. Everything else is made to
    pass so the only variable under test is the CI poll result / the
    rebase-push call.
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
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_mark_plane_done", lambda *a, **k: None,
    )


# ---------------------------------------------------------------------------
# Structural guards: the new helper and the new story field.
# ---------------------------------------------------------------------------
class TestStructure:
    def test_rebase_and_push_for_merge_helper_exists(self):
        assert hasattr(p, "_rebase_and_push_for_merge")
        assert callable(p._rebase_and_push_for_merge)

    def test_helper_signature_returns_tuple(self):
        # The helper must accept (plan_name, key, branch, worktree) and return
        # a (gate_error, pushed_sha) tuple. Calling it through the real code
        # path is exercised elsewhere; here we only assert it is importable
        # and callable with the documented arity.
        import inspect

        sig = inspect.signature(p._rebase_and_push_for_merge)
        params = list(sig.parameters)
        assert params == ["plan_name", "key", "branch", "worktree"]


# ---------------------------------------------------------------------------
# First pending observation records ci_pending_sha.
# ---------------------------------------------------------------------------
class TestFirstPendingRecordsSha:
    def test_first_pending_records_ci_pending_sha_equal_to_pushed_sha(
        self, plan_dir, monkeypatch
    ):
        pushed = "abc123firstpush"

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        def rebase_push(plan_name, key, branch, worktree):
            return ("", pushed)

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story.get("ci_pending_sha") == pushed

    def test_first_pending_records_ci_pending_sha_and_since_together(
        self, plan_dir, monkeypatch
    ):
        # Both fields are set on the same first pending observation.
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story.get("ci_pending_sha") == "deadbeefcafe"
        assert story.get("ci_pending_since") is not None

    def test_first_pending_polls_the_just_pushed_sha(self, plan_dir, monkeypatch):
        seen = []

        def ci_once(branch, *, sha):
            seen.append(sha)
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        # The first poll must be pinned to the just-pushed SHA, not the branch.
        assert seen == ["deadbeefcafe"]


# ---------------------------------------------------------------------------
# Second tick on a pending story skips the rebase/force-push.
# ---------------------------------------------------------------------------
class TestSecondTickSkipsRebase:
    def test_second_tick_does_not_call_rebase_and_push_for_merge(
        self, plan_dir, monkeypatch
    ):
        calls = {"rp": 0}

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        def rebase_push(plan_name, key, branch, worktree):
            calls["rp"] += 1
            return ("", "deadbeefcafe")

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        # Story already has a recorded pending SHA from a prior tick.
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(ci_pending_sha="recordedsha", ci_pending_since=datetime.now(timezone.utc).isoformat())},
        )

        _advance_pipeline_locked("go")
        assert calls["rp"] == 0

    def test_second_tick_polls_recorded_ci_pending_sha(
        self, plan_dir, monkeypatch
    ):
        polled = []

        def ci_once(branch, *, sha):
            polled.append(sha)
            return {"state": "pending", "error": "checks running"}

        def rebase_push(plan_name, key, branch, worktree):
            pytest.fail("_rebase_and_push_for_merge must not be called on a pending story with a recorded SHA")

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(ci_pending_sha="recordedsha", ci_pending_since=datetime.now(timezone.utc).isoformat())},
        )

        _advance_pipeline_locked("go")
        assert polled == ["recordedsha"]

    def test_second_tick_preserves_recorded_ci_pending_sha(
        self, plan_dir, monkeypatch
    ):
        # A continued-pending tick must not clobber or clear the recorded SHA.
        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        def rebase_push(plan_name, key, branch, worktree):
            return ("", "shouldnotbeused")

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(ci_pending_sha="recordedsha", ci_pending_since=datetime.now(timezone.utc).isoformat())},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story.get("ci_pending_sha") == "recordedsha"


# ---------------------------------------------------------------------------
# Resolved states clear both ci_pending_sha and ci_pending_since.
# ---------------------------------------------------------------------------
class TestResolvedClearsBoth:
    def test_resolved_pass_clears_both(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(ci_pending_sha="recordedsha", ci_pending_since=datetime.now(timezone.utc).isoformat())},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert "ci_pending_sha" not in story or story.get("ci_pending_sha") is None
        assert "ci_pending_since" not in story or story.get("ci_pending_since") is None
        # And it actually merged.
        assert story["status"] == "done"

    def test_resolved_fail_clears_both(self, plan_dir, monkeypatch):
        def ci_once(branch, *, sha):
            return {"state": "fail", "error": "tests failed"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(ci_pending_sha="recordedsha", ci_pending_since=datetime.now(timezone.utc).isoformat())},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert "ci_pending_sha" not in story or story.get("ci_pending_sha") is None
        assert "ci_pending_since" not in story or story.get("ci_pending_since") is None

    def test_resolved_fail_still_counts_against_merge_attempts(
        self, plan_dir, monkeypatch
    ):
        # Regression guard: a 'fail' result still counts exactly as before,
        # even when a ci_pending_sha was recorded.
        def ci_once(branch, *, sha):
            return {"state": "fail", "error": "tests failed"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                merge_attempts=1,
                ci_pending_sha="recordedsha",
                ci_pending_since=datetime.now(timezone.utc).isoformat(),
            )},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story["merge_attempts"] == 2

    def test_resolved_cancelled_clears_both(self, plan_dir, monkeypatch):
        # 'cancelled' (after the single rerun is already consumed) is a
        # resolved, non-pending state and must clear both fields.
        def ci_once(branch, *, sha):
            return {"state": "cancelled", "error": "aborted"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_rerun_attempted=True,
                ci_pending_sha="recordedsha",
                ci_pending_since=datetime.now(timezone.utc).isoformat(),
            )},
        )

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert "ci_pending_sha" not in story or story.get("ci_pending_sha") is None
        assert "ci_pending_since" not in story or story.get("ci_pending_since") is None


# ---------------------------------------------------------------------------
# Expiry path clears both and falls through to the merge_attempts path.
# ---------------------------------------------------------------------------
class TestExpiryPath:
    def test_expiry_clears_both_and_counts_attempt(self, plan_dir, monkeypatch):
        bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
        old = (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()

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
        story = man["stories"]["P1"]
        # Expiry clears both pending fields.
        assert "ci_pending_sha" not in story or story.get("ci_pending_sha") is None
        assert "ci_pending_since" not in story or story.get("ci_pending_since") is None
        # And falls through to the ordinary merge_attempts accounting path.
        assert story["merge_attempts"] == 2

    def test_expiry_does_not_merge(self, plan_dir, monkeypatch):
        bound = MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT
        old = (datetime.now(timezone.utc) - timedelta(seconds=bound + 60)).isoformat()
        merged = []

        def ci_once(branch, *, sha):
            return {"state": "pending", "error": "checks running"}

        _patch_merge_boundaries(monkeypatch, ci_once)
        monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
        _write_manifest(
            plan_dir, "go",
            {"P1": _story(
                ci_pending_sha="recordedsha",
                ci_pending_since=old,
            )},
        )

        _advance_pipeline_locked("go")
        assert merged == []


# ---------------------------------------------------------------------------
# Regression guard: a story with no ci_pending_sha still rebases/pushes.
# ---------------------------------------------------------------------------
class TestNoPendingShaStillRebases:
    def test_no_ci_pending_sha_calls_rebase_and_push(self, plan_dir, monkeypatch):
        calls = {"rp": 0}

        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        def rebase_push(plan_name, key, branch, worktree):
            calls["rp"] += 1
            return ("", "freshsha")

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        assert calls["rp"] == 1
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"]["status"] == "done"

    def test_empty_ci_pending_sha_still_calls_rebase_and_push(
        self, plan_dir, monkeypatch
    ):
        # A falsy ci_pending_sha (empty string) must not short-circuit.
        calls = {"rp": 0}

        def ci_once(branch, *, sha):
            return {"state": "pass", "error": ""}

        def rebase_push(plan_name, key, branch, worktree):
            calls["rp"] += 1
            return ("", "freshsha")

        _patch_merge_boundaries(monkeypatch, ci_once, rebase_push=rebase_push)
        _write_manifest(plan_dir, "go", {"P1": _story(ci_pending_sha="")})

        _advance_pipeline_locked("go")
        assert calls["rp"] == 1


# ---------------------------------------------------------------------------
# _rebase_and_push_for_merge preserves the extracted error behaviour.
# These exercise the REAL helper (not patched) with _rebase_onto_master and
# the git push subprocess mocked, so the extracted code's error contracts are
# preserved verbatim.
# ---------------------------------------------------------------------------
class TestRebaseAndPushHelperErrors:
    def _real_helper_with_mocks(self, monkeypatch, rebase_result, push_result=None,
                                worktree_exists=True):
        """Run the real ``_rebase_and_push_for_merge`` with git boundaries mocked.

        Returns ``(gate_error, pushed_sha)``. ``rebase_result`` is the dict
        ``_rebase_onto_master`` returns. ``push_result`` is a
        ``subprocess.CompletedProcess``-like for the push; when None and the
        worktree exists, the push is treated as succeeding with a rev-parse of
        ``newheadsha``.
        """
        monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, br: rebase_result)

        class _FakeProc:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if worktree_exists:
            import tempfile
            wt = tempfile.mkdtemp()
        else:
            wt = "/nonexistent-worktree"

        calls = {"push": 0, "rev": 0}

        def fake_run(argv, **kwargs):
            # The push command.
            if argv[:3] == ["git", "push", "--force-with-lease"]:
                calls["push"] += 1
                if push_result is not None:
                    return push_result
                return _FakeProc(0, "", "")
            # The rev-parse HEAD command.
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                calls["rev"] += 1
                return _FakeProc(0, "newheadsha\n", "")
            return _FakeProc(0, "", "")

        monkeypatch.setattr(p.subprocess, "run", fake_run)
        return p._rebase_and_push_for_merge("go", "P1", "agent/p1", wt), calls

    def test_rebase_failure_returns_nonempty_gate_error(self, monkeypatch):
        (gate_error, pushed_sha), _ = self._real_helper_with_mocks(
            monkeypatch,
            rebase_result={"ok": False, "conflict": True, "error": "merge conflict"},
        )
        assert gate_error  # non-empty
        assert "rebase" in gate_error
        assert pushed_sha == ""

    def test_push_failure_returns_nonempty_gate_error(self, monkeypatch):
        class _FakeProc:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        (gate_error, pushed_sha), calls = self._real_helper_with_mocks(
            monkeypatch,
            rebase_result={"ok": True, "conflict": False, "error": ""},
            push_result=_FakeProc(1, "", "remote rejected"),
        )
        assert gate_error  # non-empty
        assert "push" in gate_error
        assert pushed_sha == ""
        # The push was attempted and the rev-parse was NOT (push failed first).
        assert calls["push"] == 1
        assert calls["rev"] == 0

    def test_success_returns_pushed_sha(self, monkeypatch):
        (gate_error, pushed_sha), calls = self._real_helper_with_mocks(
            monkeypatch,
            rebase_result={"ok": True, "conflict": False, "error": ""},
        )
        assert gate_error == ""
        assert pushed_sha == "newheadsha"
        assert calls["push"] == 1
        assert calls["rev"] == 1

    def test_missing_worktree_skips_push_returns_empty_sha(self, monkeypatch):
        # A missing worktree: rebase returns ok (skipped), push is NOT
        # attempted, pushed_sha stays empty - preserved from the extracted
        # code's Path(worktree).is_dir() guard.
        (gate_error, pushed_sha), calls = self._real_helper_with_mocks(
            monkeypatch,
            rebase_result={"ok": True, "conflict": False, "error": "worktree missing - rebase skipped"},
            worktree_exists=False,
        )
        assert gate_error == ""
        assert pushed_sha == ""
        assert calls["push"] == 0
        assert calls["rev"] == 0