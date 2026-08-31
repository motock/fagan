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


# ---------------------------------------------------------------------------
# Regression: the merge gate must operate on the worktree's RESOLVED HEAD
# branch (agent/<key>-<suffix> alias), not the hardcoded convention branch.
#
# Live incident (LA-VERIFY rework-alias follow-up): a rework round leaves the
# story worktree checked out on an alias branch agent/s10-2. _open_pr and
# _merge_pr already resolve that alias (pipeline/pr.py::_resolve_story_branch),
# but _rebase_and_push_for_merge and the manual merge path
# (merge.py::_approve_merge_impl) push/CI-check the convention branch
# agent/<key> passed in from advance.py while _merge_pr merges the RESOLVED
# alias branch. When a prior _merge_pr already deleted the local convention
# branch (git branch -D), the gate's push fails with
# "src refspec agent/s10 does not match any" and the story parks at the merge
# gate forever - the tests_passed loop becomes a merge-gate loop. When a
# stale local convention branch survives instead, the gate pushes that stale
# code while _ci_status_once polls the worktree-HEAD SHA - a SHA never pushed
# to the polled branch - so CI stays pending until expiry.
#
# These tests drive a REAL git repo (a bare `origin` plus a story repo whose
# HEAD is the alias branch) so the exact refspec and failure modes reproduce.
# They fail until the gate resolves the worktree's actual HEAD branch so
# rebase -> push -> CI -> _merge_pr all operate on one branch.
# ---------------------------------------------------------------------------
def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_story_repo(tmp_path, *, delete_local_convention):
    """origin (bare) + story repo with the incident branch topology.

    Leaves HEAD on the alias branch agent/s10-2 @ alias_sha with
    origin/agent/s10-2 @ alias_sha, plus a STALE local agent/s10 that is one
    unpushed commit ahead of origin/agent/s10 - or, with
    delete_local_convention=True, the local agent/s10 branch deleted (the
    state a prior _merge_pr `git branch -D` leaves behind).

    Returns (repo, conv_origin_sha, alias_sha).
    """
    origin = tmp_path / "origin.git"
    _git("-c", "init.defaultBranch=master", "init", "--bare", "-q",
         str(origin), cwd=tmp_path)
    repo = tmp_path / "repo"
    _git("-c", "init.defaultBranch=master", "init", "-q", str(repo),
         cwd=tmp_path)
    _git("config", "user.email", "agent@local", cwd=repo)
    _git("config", "user.name", "agent", cwd=repo)

    (repo / "master.txt").write_text("m\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "m0", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "master", cwd=repo)

    # Convention branch: origin/agent/s10 @ conv_origin, then a stale local
    # commit ahead of it (code that was never pushed).
    _git("checkout", "-q", "-b", "agent/s10", cwd=repo)
    (repo / "conv.txt").write_text("conv-origin\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "conv origin", cwd=repo)
    conv_origin_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("push", "-q", "origin", "agent/s10", cwd=repo)
    (repo / "conv.txt").write_text("conv-stale-local\n")
    _git("commit", "-q", "-a", "-m", "conv stale local", cwd=repo)

    # Alias branch the rework round left checked out in the worktree.
    _git("checkout", "-q", "master", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s10-2", cwd=repo)
    (repo / "alias.txt").write_text("alias\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "alias", cwd=repo)
    alias_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("push", "-q", "origin", "agent/s10-2", cwd=repo)

    if delete_local_convention:
        _git("branch", "-q", "-D", "agent/s10", cwd=repo)

    return repo, conv_origin_sha, alias_sha


def _fake_rebase_onto_master(worktree, branch):
    # Simulate a successful Mode 9 rebase: rewrite the worktree HEAD commit
    # (new SHA) without changing which branch is checked out.
    _git("commit", "-q", "--allow-empty", "-m", "rebased onto master",
         cwd=worktree)
    return {"ok": True}


def _spy_on_git(monkeypatch):
    """Record every subprocess command while still really running it."""
    recorded = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(merge.subprocess, "run", spy)
    return recorded


def _pushed_refspecs(push_call):
    """Local refspec names pushed by one `git push` command."""
    idx = push_call.index("origin")
    return [a.lstrip("+").split(":")[0] for a in push_call[idx + 1:]]


def _call_rebase_and_push_for_merge(plan_name, key, branch, worktree):
    """Call the merge gate tolerating both fix shapes for the branch arg.

    The blocking fix may either keep the
    (plan_name, key, branch, worktree) signature - resolving the worktree HEAD
    internally and ignoring the passed convention branch - or drop the branch
    parameter entirely (advance.py stops passing it). Both shapes must
    satisfy the behavioural contract below, so dispatch on the signature.
    """
    fn = merge._rebase_and_push_for_merge
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        params = {}
    if "branch" in params:
        return fn(plan_name, key, branch, worktree)
    return fn(plan_name, key, worktree)


class TestMergeGateResolvedAliasBranch:
    def test_gate_pushes_resolved_alias_branch_not_stale_convention_branch(
        self, tmp_path, monkeypatch,
    ):
        repo, conv_origin_sha, alias_sha = _init_story_repo(
            tmp_path, delete_local_convention=False
        )
        recorded = _spy_on_git(monkeypatch)
        monkeypatch.setattr(p, "REPO_ROOT", repo)
        monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
        monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(p, "_default_branch", lambda: "master")

        gate_error, pushed_sha = _call_rebase_and_push_for_merge(
            "go", "s10", "agent/s10", str(repo)
        )

        assert gate_error == "", gate_error
        post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert post_rebase_sha != alias_sha, (
            "fake rebase must rewrite the worktree HEAD for this test to be "
            "meaningful"
        )

        push_calls = [c for c in recorded if c[:2] == ["git", "push"]]
        assert push_calls, "the merge gate must push the rebased branch"
        assert any(
            a.startswith("--force-with-lease") for a in push_calls[0]
        ), push_calls[0]
        assert _pushed_refspecs(push_calls[0]) == ["agent/s10-2"], (
            f"gate pushed {_pushed_refspecs(push_calls[0])}: it must push the "
            f"worktree's resolved HEAD branch agent/s10-2, not the hardcoded "
            f"convention branch agent/s10"
        )

        # The SHA the gate reports (and CI will poll) is the post-rebase
        # worktree-HEAD SHA that was actually pushed to the alias branch.
        assert pushed_sha == post_rebase_sha
        origin_alias_sha = _git(
            "rev-parse", "origin/agent/s10-2", cwd=repo
        ).stdout.strip()
        assert origin_alias_sha == post_rebase_sha, (
            "origin/agent/s10-2 must point at the post-rebase worktree-HEAD "
            "SHA so the CI poll queries a SHA that was actually pushed"
        )

        # The stale local convention branch (one unpushed commit ahead of
        # origin) must NOT be pushed by the gate - pushing it would land
        # un-rebased stale code on the convention remote branch.
        origin_conv_sha = _git(
            "rev-parse", "origin/agent/s10", cwd=repo
        ).stdout.strip()
        assert origin_conv_sha == conv_origin_sha, (
            "the gate must never push the stale convention branch agent/s10"
        )

    def test_gate_completes_when_convention_branch_already_deleted(
        self, tmp_path, monkeypatch,
    ):
        # Live-incident state: a prior _merge_pr already squash-merged and
        # `git branch -D`-ed the local convention branch, leaving the worktree
        # on the alias. The gate must still complete (no `src refspec` error).
        repo, _conv_origin_sha, alias_sha = _init_story_repo(
            tmp_path, delete_local_convention=True
        )
        recorded = _spy_on_git(monkeypatch)
        monkeypatch.setattr(p, "REPO_ROOT", repo)
        monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
        monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(p, "_default_branch", lambda: "master")

        gate_error, pushed_sha = _call_rebase_and_push_for_merge(
            "go", "s10", "agent/s10", str(repo)
        )

        assert "src refspec" not in gate_error, (
            f"live-incident merge-gate failure reproduced: {gate_error!r} - "
            f"the gate pushed the convention branch agent/s10 that a prior "
            f"_merge_pr had already deleted instead of the worktree's alias "
            f"HEAD"
        )
        assert gate_error == "", gate_error

        push_calls = [c for c in recorded if c[:2] == ["git", "push"]]
        assert push_calls, "the merge gate must push the rebased branch"
        assert _pushed_refspecs(push_calls[0]) == ["agent/s10-2"], (
            f"gate pushed {_pushed_refspecs(push_calls[0])}: it must push the "
            f"resolved alias branch agent/s10-2"
        )

        post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert post_rebase_sha != alias_sha, (
            "fake rebase must rewrite the worktree HEAD for this test to be "
            "meaningful"
        )
        assert pushed_sha == post_rebase_sha
        origin_alias_sha = _git(
            "rev-parse", "origin/agent/s10-2", cwd=repo
        ).stdout.strip()
        assert origin_alias_sha == post_rebase_sha, (
            "origin/agent/s10-2 must point at the post-rebase worktree-HEAD "
            "SHA so the CI poll queries a SHA that was actually pushed"
        )

    def test_advance_merge_gate_pushes_and_polls_ci_on_resolved_alias(
        self, plan_dir, tmp_path, monkeypatch,
    ):
        # End-to-end through the advance tick: with the worktree HEAD on the
        # alias and the local convention branch already deleted, the merge
        # gate must rebase, push and CI-poll the RESOLVED alias branch
        # (agent/s10-2) at the post-rebase SHA - not park the story on a
        # `src refspec` push failure (the incident), and not poll a SHA that
        # was never pushed.
        repo, _conv_origin_sha, alias_sha = _init_story_repo(
            tmp_path, delete_local_convention=True
        )
        recorded = _spy_on_git(monkeypatch)
        ci_calls = []
        notify_msgs = []

        def ci_once(branch, *, sha):
            ci_calls.append((branch, sha))
            return {"state": "success", "error": ""}

        _patch_merge_boundaries(monkeypatch, ci_once)
        monkeypatch.setattr(p, "REPO_ROOT", repo)
        monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
        monkeypatch.setattr(p, "_default_branch", lambda: "master")
        monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
        monkeypatch.setattr(
            p, "_notify_user",
            lambda *a, **k: notify_msgs.append(
                a[1] if len(a) > 1 else str(a)
            ),
        )
        for mod in (p, merge, advance):
            if hasattr(mod, "_maybe_record_retro"):
                monkeypatch.setattr(
                    mod, "_maybe_record_retro", lambda *a, **k: None
                )

        _write_manifest(plan_dir, "go", {"S10": _story(worktree=str(repo))})

        _advance_pipeline_locked("go")

        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["S10"]
        assert story["status"] == "done", (
            f"story did not clear the merge gate: "
            f"status={story['status']!r} "
            f"parked_reason={story.get('parked_reason')!r} "
            f"merge_error={story.get('merge_error')!r} "
            f"notify={notify_msgs!r}"
        )

        post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert post_rebase_sha != alias_sha, (
            "fake rebase must rewrite the worktree HEAD for this test to be "
            "meaningful"
        )

        push_calls = [c for c in recorded if c[:2] == ["git", "push"]]
        assert push_calls, "the merge gate must push the rebased branch"
        assert _pushed_refspecs(push_calls[0]) == ["agent/s10-2"], (
            f"gate pushed {_pushed_refspecs(push_calls[0])}: advance.py must "
            f"not feed the hardcoded convention branch agent/s10 into the gate"
        )

        assert ci_calls, "the gate must poll CI after pushing"
        assert ci_calls[0][0] == "agent/s10-2", (
            f"CI polled branch {ci_calls[0][0]!r}: the merge gate must "
            f"CI-check the resolved alias branch agent/s10-2 that was "
            f"actually pushed"
        )
        assert ci_calls[0][1] == post_rebase_sha, (
            f"CI polled sha {ci_calls[0][1]!r}: must be the post-rebase "
            f"worktree-HEAD sha that was actually pushed"
        )
        origin_alias_sha = _git(
            "rev-parse", "origin/agent/s10-2", cwd=repo
        ).stdout.strip()
        assert origin_alias_sha == post_rebase_sha

    def test_approve_merge_manual_path_pushes_and_ci_checks_resolved_alias(
        self, plan_dir, tmp_path, monkeypatch,
    ):
        # The human-approved merge path (merge.py::_approve_merge_impl) must
        # resolve the worktree's alias HEAD for its --force-with-lease push
        # and CI check too (same bug, second site).
        repo, _conv_origin_sha, alias_sha = _init_story_repo(
            tmp_path, delete_local_convention=True
        )
        recorded = _spy_on_git(monkeypatch)
        ci_calls = []

        def ci_status(branch, *, sha):
            ci_calls.append((branch, sha))
            return {"state": "success", "error": ""}

        monkeypatch.setattr(p, "REPO_ROOT", repo)
        monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
        monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
        monkeypatch.setattr(p, "_default_branch", lambda: "master")
        monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(
            p, "_scoped_repo_root",
            lambda plan_name: contextlib.nullcontext(),
        )
        monkeypatch.setattr(p, "_ci_status", ci_status)
        monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
        monkeypatch.setattr(
            p, "_reverify_acceptance",
            lambda story, wt, key: {"state": "pass"},
        )
        monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})
        monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
        monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
        monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
        for mod in (p, merge):
            if hasattr(mod, "_maybe_record_retro"):
                monkeypatch.setattr(
                    mod, "_maybe_record_retro", lambda *a, **k: None
                )

        _write_manifest(
            plan_dir, "go",
            {"S10": _story(worktree=str(repo), status="parked")},
        )

        result = merge._approve_merge_impl("go", "S10")

        assert result.get("ok") is True, (
            f"manual merge failed through the gate: {result!r}"
        )

        post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert post_rebase_sha != alias_sha, (
            "fake rebase must rewrite the worktree HEAD for this test to be "
            "meaningful"
        )

        push_calls = [c for c in recorded if c[:2] == ["git", "push"]]
        assert push_calls, "the manual merge path must push the rebased branch"
        assert _pushed_refspecs(push_calls[0]) == ["agent/s10-2"], (
            f"gate pushed {_pushed_refspecs(push_calls[0])}: the manual merge "
            f"path must push the resolved alias branch agent/s10-2, not "
            f"agent/s10"
        )

        assert ci_calls, "the manual merge path must CI-check the pushed branch"
        assert ci_calls[0][0] == "agent/s10-2", (
            f"CI checked branch {ci_calls[0][0]!r}: must be the resolved "
            f"alias branch agent/s10-2"
        )
        assert ci_calls[0][1] == post_rebase_sha, (
            f"CI checked sha {ci_calls[0][1]!r}: must be the post-rebase "
            f"worktree-HEAD sha that was actually pushed"
        )
        origin_alias_sha = _git(
            "rev-parse", "origin/agent/s10-2", cwd=repo
        ).stdout.strip()
        assert origin_alias_sha == post_rebase_sha