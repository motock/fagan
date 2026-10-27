"""A skipped fetch must be recorded as a stale-base dispatch, not hidden.

``_create_fresh_worktree`` only fetches ``origin/<default>`` when it wins a
non-blocking flock, then branches the worktree from ``origin/<default>``
regardless. The skip is deliberate (see
``test_dispatch_git_fetch_lock.py``: contention means the fetch is skipped and
dispatch never blocks or raises), but it used to be silent - AUTH-03
(2026-10-27) branched 32 minutes stale and re-added a module that had already
merged. These tests pin the visibility-only fix: the stale path emits a
``stale_base_dispatch`` warning naming the branch and the ref, the healthy path
still fetches once and emits nothing, and the git command shape is unchanged.
"""

import contextlib
import json
import subprocess
from unittest import mock

from pipeline import concurrency as pipeline_concurrency
from pipeline import dispatch_worktree
from pipeline import persistence as pipeline_persistence
from pipeline import server as pipeline_server


@contextlib.contextmanager
def _lock(acquired):
    """Stand-in for ``_try_acquire_git_lock`` / ``_scoped_repo_root``."""
    yield acquired


def _plan_dirs(monkeypatch, tmp_path):
    """Point every module-level plan directory at ``tmp_path``.

    ``pipeline.server``, ``pipeline.persistence`` and ``pipeline.concurrency``
    each expose their own module-level ``PLAN_DIR``; the notification sidecar is
    written through ``pipeline.persistence``'s copy.
    """
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline_persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline_concurrency, "PLAN_DIR", tmp_path)


def _records(plan_name):
    """Read back the plan's notification sidecar (missing file -> no records)."""
    path = pipeline_persistence._notifications_jsonl_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _setup(monkeypatch, tmp_path, lock_outcomes):
    """Patch every git-touching collaborator out; return the command recorder.

    ``lock_outcomes`` is consumed one entry per dispatch, so a single setup can
    drive a stale dispatch followed by a healthy one against the same plan dir.
    """
    outcomes = iter(lock_outcomes)
    commands = []

    def _record_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        dispatch_worktree,
        "_try_acquire_git_lock",
        lambda repo_root: _lock(next(outcomes)),
    )
    monkeypatch.setattr(dispatch_worktree.subprocess, "run", _record_run)
    monkeypatch.setattr(
        dispatch_worktree, "_scoped_repo_root", lambda plan_name: _lock(tmp_path)
    )
    monkeypatch.setattr(
        dispatch_worktree, "_exclude_worktree_logs_from_tracking", lambda root: None
    )
    monkeypatch.setattr(dispatch_worktree, "_provision_worktree_venv", lambda path: None)
    _plan_dirs(monkeypatch, tmp_path)
    return commands


def _patch_default_branch(monkeypatch, name="master"):
    """Patch ``_default_branch`` with a Mock so call counts are observable."""
    default_branch = mock.Mock(return_value=name)
    monkeypatch.setattr(dispatch_worktree, "_default_branch", default_branch)
    return default_branch


def test_stale_base_skips_fetch_and_emits_warning(monkeypatch, tmp_path):
    """Lock held -> no fetch, but exactly one stale_base_dispatch warning."""
    commands = _setup(monkeypatch, tmp_path, [False])
    default_branch = _patch_default_branch(monkeypatch)

    result = dispatch_worktree._create_fresh_worktree("p", "agent/s1", tmp_path / "S1")

    assert result is None
    worktree_add = [
        "git",
        "worktree",
        "add",
        "-b",
        "agent/s1",
        str(tmp_path / "S1"),
        "origin/master",
    ]
    assert commands == [worktree_add]
    assert [cmd for cmd in commands if cmd[:2] == ["git", "fetch"]] == []

    records = _records("p")
    assert len(records) == 1
    assert records[0]["event"] == "stale_base_dispatch"
    assert records[0]["severity"] == "warning"
    assert records[0]["story_key"] is None
    default_branch.assert_called_once()


def test_healthy_lock_fetches_and_does_not_add_stale_record(monkeypatch, tmp_path):
    """Lock won -> the fetch runs once, the worktree add still happens, and the
    stale record from an earlier dispatch is not joined by a second one."""
    commands = _setup(monkeypatch, tmp_path, [False, True])
    _patch_default_branch(monkeypatch)

    assert (
        dispatch_worktree._create_fresh_worktree("p", "agent/s1", tmp_path / "S1")
        is None
    )
    assert len(_records("p")) == 1

    commands.clear()
    assert (
        dispatch_worktree._create_fresh_worktree("p", "agent/s2", tmp_path / "S2")
        is None
    )
    assert commands == [
        ["git", "fetch", "origin", "master"],
        [
            "git",
            "worktree",
            "add",
            "-b",
            "agent/s2",
            str(tmp_path / "S2"),
            "origin/master",
        ],
    ]
    assert len(_records("p")) == 1


def test_stale_warning_names_branch_and_base(monkeypatch, tmp_path):
    """The operator reading the sidecar learns which story and which base."""
    _setup(monkeypatch, tmp_path, [False])
    _patch_default_branch(monkeypatch)

    assert (
        dispatch_worktree._create_fresh_worktree("p", "agent/s1", tmp_path / "S1")
        is None
    )

    records = _records("p")
    assert len(records) == 1
    assert "agent/s1" in records[0]["message"]
    assert "origin/master" in records[0]["message"]
