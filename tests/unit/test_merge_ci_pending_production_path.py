"""Production-path regression tests for the S5 non-blocking CI-pending merge gate.

The existing ``test_merge_ci_pending.py`` suite exercises the non-blocking
CI-pending behaviour by ``monkeypatch.setattr(server, "_ci_status_once", ...)``.
That monkeypatching is exactly what flips the
``_ci_status_once is not _original_ci_status_once`` identity check in
``pipeline/server.py`` and *activates* the non-blocking code path. In production
nothing ever reassigns ``pipeline.server._ci_status_once``, so the identity
check is always ``True`` (the two names refer to the same object) and the merge
gate always takes the *blocking* ``return _ci_status(branch, sha=sha)`` branch.
The non-blocking pending branch (``ci_pending_since`` / ``ci_wait = True``) is
therefore dead code in production - it only runs under test monkeypatching.

These tests reproduce that bug. They deliberately do **NOT** monkeypatch
``_ci_status_once``. Instead they mock the *underlying* CI fetch that
``_ci_status_once`` itself performs (the ``gh`` subprocess in ``pipeline.ci``)
so that the real, un-replaced ``_ci_status_once`` returns a ``pending`` result.
Against the current (buggy) code the merge gate then falls through to the
blocking ``elif ci["state"] == "pending": gate_error = ...`` branch - it sets a
``gate_error``, never sets ``ci_pending_since``, and never yields the tick -
so these tests fail for exactly the reason the reviewer described. Once the
identity-check hack is removed and the non-blocking path is the production
default, these tests pass.
"""

import json
from datetime import datetime, timezone

import pytest

import pipeline.ci as pci
import pipeline.server as p
from pipeline.server import _advance_pipeline_locked


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


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_merge_boundaries_production(monkeypatch, ci_state="pending"):
    """Stub the merge-gate boundaries WITHOUT replacing ``_ci_status_once``.

    The real, imported ``_ci_status_once`` is left in place so the production
    identity check (``_ci_status_once is _original_ci_status_once``) stays
    ``True`` - i.e. this is the production path, not the monkeypatched one.

    The CI result is controlled by mocking the ``gh`` subprocess that
    ``_ci_status_once`` calls inside ``pipeline.ci``. With ``sha=""`` (the
    worktree is a non-existent path so the merge gate never runs ``git
    rev-parse``) ``_ci_status_once`` uses the ``gh pr checks <branch>`` path,
    whose output is a JSON list of ``{"name", "bucket"}`` entries.
    """
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")

    # The blocking variant must NEVER be called on the production path once the
    # fix lands. We fail loudly if it is, which also documents the contract.
    monkeypatch.setattr(
        p, "_ci_status",
        lambda *a, **k: pytest.fail("_ci_status (blocking) called on production path"),
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
    monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, br: {"ok": True, "error": ""})
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    # Make the repo look CI-configured so an empty check-run list yields
    # "pending" rather than "none".
    monkeypatch.setattr(pci, "_repo_has_ci_configured", lambda: True)

    # Mock the gh subprocess that the REAL _ci_status_once invokes. The merge
    # gate calls _ci_status_once(branch, sha="") (worktree is non-existent so
    # no git rev-parse runs), so the gh-pr-checks branch path is taken: stdout
    # is a JSON list of {"name","bucket"} entries.
    if ci_state == "pending":
        gh_stdout = json.dumps([{"name": "ci", "bucket": "pending"}])
    elif ci_state == "pass":
        gh_stdout = json.dumps([{"name": "ci", "bucket": "pass"}])
    else:
        raise ValueError(f"unsupported ci_state {ci_state!r}")

    def fake_run(cmd, **kwargs):
        # Any gh api / gh pr checks call returns the canned CI output.
        return _FakeCompleted(returncode=0, stdout=gh_stdout, stderr="")

    monkeypatch.setattr(pci.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Production-path tests: NO monkeypatch of _ci_status_once.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _assert_no_ci_status_once_monkeypatch(monkeypatch):
    """Guardrail: these tests must never replace ``_ci_status_once``.

    Replacing it is exactly the test-only trick that activates the dead
    non-blocking path; the whole point of this file is to exercise the path
    WITHOUT that trick. If a future edit reintroduces it, fail loudly.
    """
    original = p._ci_status_once
    yield
    # After the test, the module attribute must still be the exact object that
    # was bound at import time (i.e. the production binding).
    assert p._ci_status_once is original, (
        "test illegally replaced pipeline.server._ci_status_once - this file "
        "exercises the PRODUCTION path and must not monkeypatch it"
    )


class TestMergeCiPendingProductionPath:
    """The non-blocking CI-pending behaviour must hold WITHOUT monkeypatching."""

    def test_pending_yields_tick_on_production_path(self, plan_dir, monkeypatch):
        # A pending CI result, observed through the REAL _ci_status_once (the
        # underlying gh fetch is mocked, not _ci_status_once itself), must yield
        # the tick back to the scheduler rather than blocking with a gate_error.
        _patch_merge_boundaries_production(monkeypatch, ci_state="pending")
        _write_manifest(plan_dir, "go", {"P1": _story()})

        result = _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]

        # The non-blocking contract: pending yields the tick (key reported as
        # ci_pending) and does NOT raise a gate_error that would consume a merge
        # attempt / park / fail the story.
        assert "ci_pending" in result
        assert "P1" in result["ci_pending"], (
            "pending CI on the production path must yield the tick "
            "(appear in summary['ci_pending']), but it did not"
        )
        assert story["status"] == "pr_open", (
            f"pending CI must not advance the story off pr_open, got {story['status']!r}"
        )

    def test_pending_sets_ci_pending_since_on_production_path(self, plan_dir, monkeypatch):
        # The very first pending observation must record ci_pending_since so a
        # later tick can evaluate the wait timeout against elapsed time. Under
        # the buggy production code the blocking branch never sets it.
        _patch_merge_boundaries_production(monkeypatch, ci_state="pending")
        _write_manifest(plan_dir, "go", {"P1": _story()})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        assert man["stories"]["P1"].get("ci_pending_since") is not None, (
            "pending CI on the production path must set ci_pending_since on the "
            "first observation so the wait timeout can be evaluated across ticks"
        )

    def test_pending_does_not_set_gate_error_on_production_path(self, plan_dir, monkeypatch):
        # On the buggy production path, pending falls through to
        # `elif ci["state"] == "pending": gate_error = ...`, which consumes a
        # merge attempt. The non-blocking contract must NOT set a gate_error for
        # a pending result. We detect this by confirming merge_attempts is not
        # incremented and the story is not parked/failed.
        _patch_merge_boundaries_production(monkeypatch, ci_state="pending")
        _write_manifest(plan_dir, "go", {"P1": _story(merge_attempts=2)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        assert story["merge_attempts"] == 2, (
            "pending CI on the production path must not consume a merge attempt "
            "(non-blocking tick), but merge_attempts changed"
        )
        assert story.get("parked_reason") is None and story["status"] != "parked", (
            "pending CI on the production path must not park the story"
        )

    def test_production_path_does_not_call_blocking_ci_status(self, plan_dir, monkeypatch):
        # The production path must route through _ci_status_once, never the
        # blocking _ci_status. The boundary patch fails the test if the blocking
        # variant is called. With a pending result the tick must still yield.
        _patch_merge_boundaries_production(monkeypatch, ci_state="pending")
        _write_manifest(plan_dir, "go", {"P1": _story()})

        # If the implementation still calls the blocking _ci_status on the
        # production path, the patched stub raises -> test fails for the right
        # reason (the bug). Once fixed, this runs the non-blocking path and the
        # assertion below holds.
        result = _advance_pipeline_locked("go")
        assert "P1" in result.get("ci_pending", []), (
            "production path must use the non-blocking _ci_status_once and yield "
            "the tick for a pending result"
        )

    def test_subsequent_pass_clears_ci_pending_since_on_production_path(self, plan_dir, monkeypatch):
        # Edge case from the review: ci_pending_since set on the first pending
        # tick must persist correctly and be cleared once CI later passes, all
        # on the production path (no _ci_status_once monkeypatch).
        first = datetime.now(timezone.utc).isoformat()
        _patch_merge_boundaries_production(monkeypatch, ci_state="pass")
        _write_manifest(plan_dir, "go", {"P1": _story(ci_pending_since=first)})

        _advance_pipeline_locked("go")
        man = _read_manifest(plan_dir, "go")
        story = man["stories"]["P1"]
        # A passing CI must clear the pending marker and proceed to merge.
        assert "ci_pending_since" not in story or story.get("ci_pending_since") is None, (
            "a passing CI result on the production path must clear ci_pending_since"
        )