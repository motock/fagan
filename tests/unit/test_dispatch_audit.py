"""A dispatch records the commit its worktree was cut from (AUD-04).

The manifest keeps ``branch``, ``worktree`` and ``correlation_id``, but nothing
recorded which ``origin/<default>`` tip the agent's first commit sat on - the
gap the AUTH-03 incident (2026-10-27) exposed when a story branched 32 minutes
stale and re-added a module that had already merged. ``_create_fresh_worktree``
now appends one ``story.dispatch`` record carrying ``base_sha`` and
``fetch_outcome``, and records the stale-base refusal as a governance event.

The base-sha read is best-effort by contract: the dispatch tests in this repo
substitute ``subprocess.run`` with a recorder that returns ``None``, so an
unreadable base is recorded as unknown rather than failing a dispatch whose
worktree already exists.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from app.identity import Principal
from pipeline import audit as pipeline_audit
from pipeline import concurrency as pipeline_concurrency
from pipeline import dispatch_worktree
from pipeline import persistence as pipeline_persistence
from pipeline import server as pipeline_server
from pipeline.actor import bind_correlation_id, bind_principal

#: Alias so each test below names the unit under test directly.
_create_fresh_worktree = dispatch_worktree._create_fresh_worktree


@contextlib.contextmanager
def _ctx(value):
    """Stand-in for ``_try_acquire_git_lock`` / ``_scoped_repo_root``."""
    yield value


def _plan_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every module-level plan directory at ``tmp_path``."""
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline_persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline_concurrency, "PLAN_DIR", tmp_path)


def _audit_records(plan_name: str) -> list[dict]:
    """Read back the plan's audit stream (missing file -> no records)."""
    path = pipeline_audit.audit_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lock_outcomes: list[bool],
    *,
    head: str | None = "abc123\n",
    worktree_add_fails: bool = False,
) -> list[list[str]]:
    """Patch every git-touching collaborator out; return the command recorder.

    ``lock_outcomes`` is consumed one entry per dispatch. ``head`` is the stdout
    of the fake ``git rev-parse HEAD`` (``None`` makes it raise), and
    ``worktree_add_fails`` makes ``git worktree add`` exit non-zero.
    """
    outcomes = iter(lock_outcomes)
    commands: list[list[str]] = []

    def _record_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            if head is None:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, head, "")
        if cmd[:3] == ["git", "worktree", "add"] and worktree_add_fails:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        dispatch_worktree, "_try_acquire_git_lock", lambda repo_root: _ctx(next(outcomes))
    )
    monkeypatch.setattr(dispatch_worktree.subprocess, "run", _record_run)
    monkeypatch.setattr(
        dispatch_worktree, "_scoped_repo_root", lambda plan_name: _ctx(tmp_path)
    )
    monkeypatch.setattr(
        dispatch_worktree, "_exclude_worktree_logs_from_tracking", lambda root: None
    )
    monkeypatch.setattr(dispatch_worktree, "_provision_worktree_venv", lambda path: None)
    monkeypatch.setattr(dispatch_worktree, "_default_branch", lambda: "master")
    _plan_dirs(monkeypatch, tmp_path)
    return commands


# --------------------------------------------------------------------------
# The allowed dispatch record
# --------------------------------------------------------------------------


def test_dispatch_writes_exactly_one_audit_record(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert len(_audit_records("p")) == 1


def test_allowed_record_action_is_story_dispatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["action"] == "story.dispatch"


def test_lock_acquired_records_fetch_outcome_fetched(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    record = _audit_records("p")[0]
    assert (record["outcome"], record["detail"]["fetch_outcome"]) == (
        "allowed",
        "fetched",
    )


def test_lock_acquired_records_base_sha_from_head(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True], head="abc123\n")

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["detail"]["base_sha"] == "abc123"


def test_lock_held_records_fetch_outcome_stale_skipped(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [False])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    record = _audit_records("p")[0]
    assert (record["outcome"], record["detail"]["fetch_outcome"]) == (
        "allowed",
        "stale_skipped",
    )


# --------------------------------------------------------------------------
# An unreadable base never fails an already-provisioned dispatch
# --------------------------------------------------------------------------


def test_failing_base_sha_read_does_not_fail_dispatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True], head=None)

    assert _create_fresh_worktree("p", "agent/s1", tmp_path / "S1") is None


def test_failing_base_sha_read_records_unknown_base(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True], head=None)

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    record = _audit_records("p")[0]
    assert (record["outcome"], record["detail"]["base_sha"]) == ("allowed", None)


# --------------------------------------------------------------------------
# Attribution: story key, falling back to the branch for older callers
# --------------------------------------------------------------------------


def test_story_key_is_recorded_as_target_story(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1", story_key="STALE-01")

    assert _audit_records("p")[0]["target"]["story"] == "STALE-01"


def test_target_story_falls_back_to_branch_name(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["target"]["story"] == "agent/s1"


# --------------------------------------------------------------------------
# No record for a dispatch that never provisioned a worktree
# --------------------------------------------------------------------------


def test_setup_failure_writes_no_dispatch_record(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True], worktree_add_fails=True)

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert [
        record
        for record in _audit_records("p")
        if record["action"] == "story.dispatch"
    ] == []


# --------------------------------------------------------------------------
# A provably stale base is a refusal, and refusals are audited
# --------------------------------------------------------------------------


@pytest.fixture()
def stale_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make STALE-01's probe see a remote tip ahead of this checkout's cache."""
    monkeypatch.setattr(dispatch_worktree, "_remote_tip", lambda root, branch: "beef" * 10)
    monkeypatch.setattr(
        dispatch_worktree, "_cached_remote_tip", lambda root, branch: "cafe" * 10
    )


def test_stale_base_refusal_is_recorded(monkeypatch, tmp_path, stale_probes):
    _setup(monkeypatch, tmp_path, [False])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    records = _audit_records("p")
    assert (len(records), records[0]["outcome"]) == (1, "refused")


def test_stale_base_refusal_records_reason(monkeypatch, tmp_path, stale_probes):
    _setup(monkeypatch, tmp_path, [False])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["detail"]["reason"] == "stale_base"


def test_stale_base_refusal_never_adds_a_worktree(monkeypatch, tmp_path, stale_probes):
    commands = _setup(monkeypatch, tmp_path, [False])

    _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert [cmd for cmd in commands if cmd[:3] == ["git", "worktree", "add"]] == []


# --------------------------------------------------------------------------
# The record is attributable to the request that caused it
# --------------------------------------------------------------------------


def test_record_actor_is_bound_principal_subject(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal), bind_correlation_id("req-4"):
        _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["actor"] == "alice"


def test_record_correlation_id_is_bound_correlation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [True])
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal), bind_correlation_id("req-4"):
        _create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert _audit_records("p")[0]["correlation_id"] == "req-4"
