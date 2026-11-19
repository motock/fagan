"""Unit tests for ``pipeline.worktree_cleanup.remove_finished_worktree``.

The cleanup runs real ``git`` commands against real repositories created
under ``tmp_path`` - the behaviour under test (which branch is deleted, what
is left on disk, whether the remote is touched) is only observable through
the filesystem and the git refs.

Contract exercised here (see the story brief):

* one outcome token is returned, and the function never raises;
* ``""`` / a missing path -> ``absent``;
* a path outside ``worktree_root`` -> ``outside_root`` and nothing is
  touched;
* a worktree with tracked modifications other than ``agent.log`` -> ``dirty``
  and the worktree is kept;
* a clean worktree (only ``agent.log`` modified) -> ``removed``: the
  worktree directory and the local branch are gone;
* an alias branch (``agent/<key>-<suffix>``) is the branch that gets deleted;
* a failing ``git worktree remove`` -> ``failed`` (no exception);
* the remote branch is NEVER deleted - this path makes no outward change.
"""

import logging
import subprocess

import pytest

from pipeline.worktree_cleanup import remove_finished_worktree

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_env(tmp_path):
    """A git repo with one commit plus a worktree root directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "chore: init", cwd=repo)

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    return repo, worktree_root


def _add_worktree(repo, worktree_root, name="k1", branch="agent/k1"):
    """Create ``<worktree_root>/<name>`` on a new branch and return its path."""
    path = worktree_root / name
    _git("worktree", "add", "-b", branch, str(path), cwd=repo)
    return path


def _branches(repo, pattern=None):
    out = _git("branch", "--list", *( [pattern] if pattern else [] ), cwd=repo).stdout
    return [line.strip().lstrip("* ").strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_clean_worktree_is_removed_and_its_branch_deleted(git_env):
    """A clean worktree inside the root -> 'removed'; dir and branch gone."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "removed"
    assert not wt.exists(), "the worktree directory must be gone after removal"
    assert _branches(repo, "agent/k1") == [], "the local branch must be deleted"


def test_alias_branch_is_the_branch_that_gets_deleted(git_env):
    """A worktree sitting on an alias branch deletes THAT branch."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    _git("checkout", "-b", "agent/k1-rework", cwd=wt)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "removed"
    assert not wt.exists()
    assert _branches(repo, "agent/k1-rework") == [], "the alias branch must be deleted"


def test_only_agent_log_modified_still_removes(git_env):
    """agent.log is the pipeline's own log: it never blocks the cleanup."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    (wt / "agent.log").write_text("noise\n")
    _git("add", "agent.log", cwd=wt)
    _git("commit", "-m", "chore: add agent log", cwd=wt)
    (wt / "agent.log").write_text("more noise\n")

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "removed"
    assert not wt.exists()


def test_remote_branch_is_never_deleted(git_env, tmp_path):
    """The PR merge owns the remote branch - cleanup makes no outward change."""
    repo, worktree_root = git_env
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", cwd=origin)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    wt = _add_worktree(repo, worktree_root)
    _git("push", "origin", "agent/k1", cwd=repo)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "removed"
    remote_branches = _git("branch", "--list", "agent/k1", cwd=origin).stdout.strip()
    assert remote_branches, "the remote branch must still exist after cleanup"


# ---------------------------------------------------------------------------
# negative / boundary cases
# ---------------------------------------------------------------------------


def test_dirty_worktree_is_kept(git_env):
    """A modified tracked file -> 'dirty'; the worktree is left in place."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    (wt / "README.md").write_text("changed\n")

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "dirty"
    assert wt.exists(), "a dirty worktree must be kept"
    assert (wt / "README.md").read_text() == "changed\n"
    assert _branches(repo, "agent/k1"), "a dirty worktree's branch must be kept"


def test_dirty_worktree_logs_a_warning_without_file_contents(git_env, caplog):
    """The WARNING names the story and the changed-path count, never content."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    secret = "SECRET_FILE_CONTENT_MARKER"
    (wt / "README.md").write_text(secret + "\n")

    with caplog.at_level(logging.DEBUG):
        outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "dirty"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a dirty worktree must log a WARNING"
    blob = " ".join(
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records
    )
    assert secret not in blob, "file contents must never be logged"
    assert "k1" in blob, "the warning must identify the story"


def test_path_outside_the_root_is_never_touched(git_env, tmp_path):
    """A worktree outside WORKTREE_ROOT -> 'outside_root'; nothing happens."""
    repo, worktree_root = git_env
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    wt = outside / "k1"
    _git("worktree", "add", "-b", "agent/k1", str(wt), cwd=repo)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "outside_root"
    assert wt.exists(), "a worktree outside the root must be left alone"
    assert _branches(repo, "agent/k1"), "the branch of an outside worktree must be kept"


def test_empty_worktree_path_is_absent(git_env):
    """An empty path -> 'absent'."""
    repo, worktree_root = git_env

    outcome = remove_finished_worktree(repo, "", "k1", worktree_root=worktree_root)

    assert outcome == "absent"


def test_missing_worktree_path_is_absent(git_env):
    """A path that does not exist -> 'absent'."""
    repo, worktree_root = git_env

    outcome = remove_finished_worktree(
        repo, str(worktree_root / "nope"), "k1", worktree_root=worktree_root
    )

    assert outcome == "absent"


def test_failed_worktree_remove_reports_failed_without_raising(git_env, monkeypatch):
    """A failing `git worktree remove` -> 'failed'; no exception escapes."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv)[:3] == ["git", "worktree", "remove"]:
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "failed"
    assert wt.exists(), "a failed removal must leave the worktree in place"


def test_failing_status_command_reports_dirty(git_env, monkeypatch):
    """If the status probe fails the worktree is treated as dirty, not removed."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv)[:2] == ["git", "status"]:
            raise OSError("git status exploded")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "dirty"
    assert wt.exists()


def test_branch_delete_failure_still_counts_as_removed(git_env, monkeypatch):
    """The branch may already be gone: that is still a successful cleanup."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv)[:3] == ["git", "branch", "-D"]:
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="no such branch")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    outcome = remove_finished_worktree(repo, str(wt), "k1", worktree_root=worktree_root)

    assert outcome == "removed"
    assert not wt.exists()


def test_worktree_root_is_keyword_only(git_env):
    """worktree_root is keyword-only: passing it positionally is a TypeError."""
    repo, worktree_root = git_env
    wt = _add_worktree(repo, worktree_root)

    with pytest.raises(TypeError):
        remove_finished_worktree(repo, str(wt), "k1", worktree_root)
