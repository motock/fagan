"""A provably stale base must refuse the fresh dispatch, not just warn.

``_create_fresh_worktree`` fetches ``origin/<default>`` when it wins the
non-blocking git lock. When it loses, DW-01 records a ``stale_base_dispatch``
warning and branches from whatever ``origin/<default>`` is cached locally -
which is how AUTH-03 (2026-10-27) branched 32 minutes stale and re-added a
module that had already merged.

These tests pin the read-only freshness probe on that skip path only:
``git ls-remote`` + ``git rev-parse --verify`` answer "is the cached ref the
remote tip?" without writing a ref or taking the git lock. A provable mismatch
refuses the dispatch; an unanswerable probe (outage, no origin, missing branch)
keeps today's warn-and-proceed behaviour, because an outage is not the story's
fault. The healthy path must never probe - after a real fetch the cached ref IS
the remote tip.
"""

import contextlib
import json
import subprocess

from pipeline import concurrency as pipeline_concurrency
from pipeline import dispatch_worktree
from pipeline import persistence as pipeline_persistence
from pipeline import server as pipeline_server

#: What the remote reports from ``git ls-remote`` on a stale base.
REMOTE_SHA = "ffee000011112222333344445555666677778888"
#: What this checkout has cached in ``refs/remotes/origin/<default>``.
CACHED_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@contextlib.contextmanager
def _lock(value):
    """Stand-in for ``_try_acquire_git_lock`` / ``_scoped_repo_root``."""
    yield value


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


class _Git:
    """Stand-in for ``subprocess.run``: one independently-knobbled git reply each.

    Dispatches on the command prefix so the remote probe, the cached-ref read,
    the fetch and the worktree add can each be made to succeed or fail on their
    own. ``check=True`` callers see a ``CalledProcessError`` on a non-zero exit,
    exactly as real ``subprocess.run`` would raise.
    """

    def __init__(
        self,
        *,
        lock_acquired,
        remote_sha=REMOTE_SHA,
        cached_sha=CACHED_SHA,
        ls_remote_returncode=0,
        ls_remote_stdout=None,
        rev_parse_returncode=0,
    ):
        self.lock_acquired = lock_acquired
        self.cached_sha = cached_sha
        self.ls_remote_returncode = ls_remote_returncode
        self.ls_remote_stdout = (
            f"{remote_sha}\trefs/heads/master"
            if ls_remote_stdout is None
            else ls_remote_stdout
        )
        self.rev_parse_returncode = rev_parse_returncode
        self.commands = []

    def __call__(self, cmd, *args, **kwargs):
        self.commands.append(cmd)
        returncode, stdout = self._answer(cmd)
        if returncode:
            raise subprocess.CalledProcessError(returncode, cmd, output=stdout)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    def _answer(self, cmd):
        if cmd[:2] == ["git", "ls-remote"]:
            return self.ls_remote_returncode, self.ls_remote_stdout
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return self.rev_parse_returncode, self.cached_sha
        return 0, ""


def _setup(monkeypatch, tmp_path, git):
    """Patch every git-touching collaborator out and return the ``_Git`` fake."""
    monkeypatch.setattr(
        dispatch_worktree,
        "_try_acquire_git_lock",
        lambda repo_root: _lock(git.lock_acquired),
    )
    monkeypatch.setattr(dispatch_worktree.subprocess, "run", git)
    monkeypatch.setattr(
        dispatch_worktree, "_scoped_repo_root", lambda plan_name: _lock(tmp_path)
    )
    monkeypatch.setattr(
        dispatch_worktree, "_exclude_worktree_logs_from_tracking", lambda root: None
    )
    monkeypatch.setattr(dispatch_worktree, "_provision_worktree_venv", lambda path: None)
    monkeypatch.setattr(dispatch_worktree, "_default_branch", lambda: "master")
    _plan_dirs(monkeypatch, tmp_path)
    return git


def _dispatch(tmp_path):
    """Drive the fresh-worktree path directly, as the scheduler's tick would."""
    return dispatch_worktree._create_fresh_worktree("p", "agent/s1", tmp_path / "S1")


def _worktree_adds(git):
    return [cmd for cmd in git.commands if cmd[:3] == ["git", "worktree", "add"]]


def _worktree_add(tmp_path):
    return [
        "git",
        "worktree",
        "add",
        "-b",
        "agent/s1",
        str(tmp_path / "S1"),
        "origin/master",
    ]


def test_stale_base_refusal_names_branch_and_both_shas(monkeypatch, tmp_path):
    """A provable mismatch refuses, naming the branch and both tips (12 chars)."""
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False))

    result = _dispatch(tmp_path)

    assert result["ok"] is False
    assert all(
        needle in result["error"]
        for needle in ("agent/s1", CACHED_SHA[:12], REMOTE_SHA[:12])
    )


def test_stale_base_refusal_issues_no_worktree_add(monkeypatch, tmp_path):
    """The refusal lands before the worktree is created: no ``worktree add``."""
    git = _setup(monkeypatch, tmp_path, _Git(lock_acquired=False))

    _dispatch(tmp_path)

    assert _worktree_adds(git) == []


def test_stale_base_refusal_keeps_both_sidecar_records(monkeypatch, tmp_path):
    """The existing warning survives, joined by a warning-severity refusal."""
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False))

    _dispatch(tmp_path)

    assert [
        (record["event"], record["severity"], record["story_key"])
        for record in _records("p")
    ] == [
        ("stale_base_dispatch", "warning", None),
        ("stale_base_refused", "warning", None),
    ]


def test_current_base_is_not_refused(monkeypatch, tmp_path):
    """A cached ref that IS the remote tip is not stale: dispatch proceeds."""
    git = _setup(monkeypatch, tmp_path, _Git(lock_acquired=False, remote_sha=CACHED_SHA))

    result = _dispatch(tmp_path)

    assert (result, _worktree_adds(git)) == (None, [_worktree_add(tmp_path)])


def test_remote_probe_failure_fails_open(monkeypatch, tmp_path):
    """An unreachable remote is "unknown", never "stale": warn and proceed."""
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False, ls_remote_returncode=128))

    result = _dispatch(tmp_path)

    assert (result, [record["event"] for record in _records("p")]) == (
        None,
        ["stale_base_dispatch"],
    )


def test_remote_branch_absent_fails_open(monkeypatch, tmp_path):
    """``ls-remote`` succeeding with empty stdout means no such branch: unknown."""
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False, ls_remote_stdout=""))

    result = _dispatch(tmp_path)

    assert result is None


def test_cached_ref_unreadable_fails_open(monkeypatch, tmp_path):
    """Without a readable cached ref there is nothing to compare: unknown."""
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False, rev_parse_returncode=1))

    result = _dispatch(tmp_path)

    assert result is None


def test_healthy_lock_path_never_probes(monkeypatch, tmp_path):
    """After a successful fetch the cached ref IS the remote tip: no probe."""
    git = _setup(monkeypatch, tmp_path, _Git(lock_acquired=True))

    _dispatch(tmp_path)

    assert [cmd for cmd in git.commands if cmd[:2] == ["git", "ls-remote"]] == []
    assert [
        cmd
        for cmd in git.commands
        if cmd[:3] == ["git", "rev-parse", "--verify"]
    ] == []
    assert ["git", "fetch", "origin", "master"] in git.commands


def test_probe_result_without_stdout_is_unknown_not_a_crash(monkeypatch, tmp_path):
    """A ``subprocess.run`` result with no ``stdout`` means "unknown", not a crash.

    Roughly thirty dispatch tests substitute ``subprocess.run`` with a recorder
    that returns ``None``. They passed only because the stale branch runs just
    while another worker holds the advisory git lock, so the unguarded
    ``completed.stdout`` read surfaced rarely (PR #1309, Python 3.12 job): the
    warning fired, the probe raised ``AttributeError``, and the dispatch died
    instead of proceeding with an unknown base. Two consecutive unknown probes
    must also leave nothing poisoned behind for the next, readable one.
    """
    _setup(monkeypatch, tmp_path, _Git(lock_acquired=False))
    monkeypatch.setattr(dispatch_worktree.subprocess, "run", lambda *args, **kwargs: None)

    first = _dispatch(tmp_path)
    second = _dispatch(tmp_path)

    assert (first, second) == (None, None)

    def _readable(cmd, *args, **kwargs):
        """A real ``CompletedProcess`` again, with a readable, current tip."""
        stdout = "abc1234\trefs/heads/master" if cmd[:2] == ["git", "ls-remote"] else "abc1234"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(dispatch_worktree.subprocess, "run", _readable)

    third = _dispatch(tmp_path)

    assert dispatch_worktree._remote_tip(tmp_path, "master") == "abc1234"
    assert third is None
