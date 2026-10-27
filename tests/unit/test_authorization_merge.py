"""AUTH-03: authorization on plan pause/resume and story merges.

Two of the four enforcement points live in ``pipeline/merge.py``:

* ``_set_plan_paused`` -- the single control behind both pause and resume.
  ADR 0009's vocabulary has one action for both directions, so both are
  checked as ``plan:pause``; the direction is visible in the caller.
* ``_approve_merge_impl`` -- the explicit human merge.

The contract these tests pin down:

* A caller bound to ``ANONYMOUS`` (the dashboard chat-stream ``k-``
  pass-through) is refused, and the refusal is a no-op: the manifest on disk
  is unchanged and no git/CI command runs.
* The unattended path is unaffected: the scheduler and the daemon run as the
  process itself, so ``current_principal()`` is ``LOCAL_OPERATOR`` and every
  action is allowed. Nothing bound resolves to ``LOCAL_OPERATOR`` too.
* ``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch and works on
  this path as well.
* Each refusal appends exactly one ``authorization_denied`` record to the
  plan's notification sidecar -- and an allowed call appends none, so the
  guard must not log a denial of its own on top of ``authorize``'s.

``PIPELINE_POLICY_ENFORCE`` is read on every call
(``pipeline/authorization.py:49`` -> ``enforcement_enabled``), not cached at
import time, so ``monkeypatch.setenv`` is observed at call time.

``plan_dir`` is the shared fixture from ``tests/unit/conftest.py``, which
points the ``pipeline.server``, ``pipeline.persistence`` and
``pipeline.concurrency`` PLAN_DIR bindings at one tmp directory -- the same
bindings the store and the denial sidecar read through.
"""

import json
import subprocess

import pytest

from app.identity import LOCAL_OPERATOR
from pipeline import merge as merge_mod
from pipeline import server as server_mod
from pipeline.actor import ANONYMOUS, bind_principal

PLAN = "p"
STORY = "S1"
DENIAL_EVENT = "authorization_denied"


def _manifest(*, paused=False):
    return {
        "repo_root": "/tmp/repo",
        "paused": paused,
        "stories": {
            STORY: {
                "summary": "do the thing",
                "status": "parked",
                "review_verdict": "APPROVE",
                "risk": "low",
                "worktree": "",
            }
        },
    }


def _write_manifest(plan_dir, *, paused=False):
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(_manifest(paused=paused)))


def _manifest_bytes(plan_dir):
    """The manifest exactly as it sits on disk -- the no-op assertion."""
    return (plan_dir / f"{PLAN}.manifest.json").read_bytes()


def _read_manifest(plan_dir):
    """Read the manifest back from disk -- the state assertion, not the return."""
    return json.loads(_manifest_bytes(plan_dir))


def _denial_records(plan_dir):
    """Every ``authorization_denied`` record in the plan's JSONL sidecar."""
    path = plan_dir / f"{PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [r for r in records if r.get("event") == DENIAL_EVENT]


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------
def test_anonymous_pause_is_refused_and_manifest_unchanged(plan_dir):
    _write_manifest(plan_dir, paused=False)
    before = _manifest_bytes(plan_dir)

    with bind_principal(ANONYMOUS):
        result = merge_mod._set_plan_paused(PLAN, True)

    assert result["ok"] is False
    assert "plan:pause" in result["error"]
    assert _read_manifest(plan_dir)["paused"] is False
    assert _manifest_bytes(plan_dir) == before


def test_anonymous_resume_is_refused_and_manifest_still_paused(plan_dir):
    """Resume is the same control, so it is the same ``plan:pause`` action."""
    _write_manifest(plan_dir, paused=True)
    before = _manifest_bytes(plan_dir)

    with bind_principal(ANONYMOUS):
        result = merge_mod._set_plan_paused(PLAN, False)

    assert result["ok"] is False
    assert "plan:pause" in result["error"]
    assert _read_manifest(plan_dir)["paused"] is True
    assert _manifest_bytes(plan_dir) == before


def test_local_operator_pause_is_allowed_and_persisted(plan_dir):
    _write_manifest(plan_dir, paused=False)

    with bind_principal(LOCAL_OPERATOR):
        result = merge_mod._set_plan_paused(PLAN, True)

    assert result.get("ok") is True
    assert _read_manifest(plan_dir)["paused"] is True


def test_nothing_bound_pause_is_allowed_and_persisted(plan_dir):
    """Nothing bound resolves to ``LOCAL_OPERATOR`` -- the unattended path."""
    _write_manifest(plan_dir, paused=False)

    result = merge_mod._set_plan_paused(PLAN, True)

    assert result.get("ok") is True
    assert _read_manifest(plan_dir)["paused"] is True


def test_enforcement_disabled_allows_anonymous_pause(plan_dir, monkeypatch):
    """``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch."""
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")
    _write_manifest(plan_dir, paused=False)

    with bind_principal(ANONYMOUS):
        result = merge_mod._set_plan_paused(PLAN, True)

    assert result.get("ok") is True
    assert _read_manifest(plan_dir)["paused"] is True


# ---------------------------------------------------------------------------
# Story merge
# ---------------------------------------------------------------------------
def test_anonymous_merge_is_refused_without_running_git_or_ci(plan_dir, monkeypatch):
    _write_manifest(plan_dir, paused=False)
    before = _manifest_bytes(plan_dir)

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess ran")

    monkeypatch.setattr(subprocess, "run", _boom)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("merge helper ran")

    for name in (
        "_merge_pr",
        "_ci_status",
        "_ci_rerun",
        "_rebase_onto_master",
        "_reverify_acceptance",
        "_reverify_build",
        "_mark_plane_done",
        "_mcp_restart_notice",
        "_mcp_self_source_touched",
        "_default_branch",
        "_scoped_repo_root",
        "_atomic_write_json",
    ):
        monkeypatch.setattr(server_mod, name, _must_not_run)

    with bind_principal(ANONYMOUS):
        result = merge_mod._approve_merge_impl(PLAN, STORY)

    assert result["ok"] is False
    assert "story:merge" in result["error"]
    assert _manifest_bytes(plan_dir) == before


# ---------------------------------------------------------------------------
# Unvalidated keys
# ---------------------------------------------------------------------------
def test_anonymous_merge_with_traversal_plan_name_writes_nothing(plan_dir):
    """The guard must sit *after* ``_validate_key``, never before it.

    ``authorize`` is not inert on bad input: it reads the manifest by
    ``plan_name`` and, on denial, appends an ``authorization_denied`` record to
    ``PLAN_DIR / f"{plan_name}.notifications.jsonl"``. If the guard ran on an
    unvalidated ``plan_name``, an ANONYMOUS caller -- the transport this change
    targets -- could have that append land outside PLAN_DIR.
    """
    escapee = plan_dir.parent / "escape.notifications.jsonl"

    with bind_principal(ANONYMOUS), pytest.raises(ValueError):
        merge_mod._approve_merge_impl("../escape", STORY)

    assert not escapee.exists()
    assert list(plan_dir.iterdir()) == []


def test_anonymous_pause_with_traversal_plan_name_writes_nothing(plan_dir):
    """Same boundary check on the pause control, which validates in-module."""
    escapee = plan_dir.parent / "escape.notifications.jsonl"

    with bind_principal(ANONYMOUS), pytest.raises(ValueError):
        merge_mod._set_plan_paused("../escape", True)

    assert not escapee.exists()
    assert list(plan_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Denial records
# ---------------------------------------------------------------------------
def test_each_refusal_appends_exactly_one_denial_record(plan_dir):
    """A refusal records once; an allowed call records nothing.

    The mistake this guards against: computing the refusal correctly but
    letting the function fall through and write the manifest anyway, or
    logging a second denial in ``merge.py`` on top of ``authorize``'s.
    """
    _write_manifest(plan_dir, paused=False)
    assert _denial_records(plan_dir) == []

    with bind_principal(ANONYMOUS):
        refused_pause = merge_mod._set_plan_paused(PLAN, True)
    assert refused_pause["ok"] is False
    assert len(_denial_records(plan_dir)) == 1
    assert _read_manifest(plan_dir)["paused"] is False

    with bind_principal(LOCAL_OPERATOR):
        allowed_pause = merge_mod._set_plan_paused(PLAN, True)
    assert allowed_pause.get("ok") is True
    assert len(_denial_records(plan_dir)) == 1
    assert _read_manifest(plan_dir)["paused"] is True

    with bind_principal(ANONYMOUS):
        refused_merge = merge_mod._approve_merge_impl(PLAN, STORY)
    assert refused_merge["ok"] is False
    assert len(_denial_records(plan_dir)) == 2
