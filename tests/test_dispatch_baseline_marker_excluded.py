"""Regression guard: the baseline-snapshot marker must never be trackable.

`pipeline/dispatch.py` writes `.dispatch_baseline_test_checked` into a
freshly created worktree BEFORE the test-author phase and the executor run.
That makes it exposed to every `_commit_wip` (`git add -A`) in the story's
life: the first WIP commit sweeps it into the story's history, the pre-merge
rebase then replays it and refuses on a dirty tree, terminal-failing an
otherwise-green story. This is the Mode 17 failure documented at
`pipeline/paths.py:30-52`, and `.tdd_split_test_author_done` (`.gitignore:28`)
is the sibling marker that already guards against it.

Both halves of the guard are asserted here: the repo `.gitignore` entry (so
the marker is ignored in the pipeline repo itself) and the
`_WORKTREE_LOG_EXCLUDES` entry (so every dispatched worktree of any repo gets
it written into `.git/info/exclude`).
"""
import subprocess
from pathlib import Path

import pytest

from pipeline import paths

_MARKER = ".dispatch_baseline_test_checked"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "master", ".", cwd=path)
    _git("config", "user.email", "t@t.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    return path


def test_marker_is_in_repo_gitignore():
    """The marker must be ignored in the pipeline repo itself."""
    lines = {
        line.strip()
        for line in (_REPO_ROOT / ".gitignore").read_text().splitlines()
    }
    assert _MARKER in lines, (
        f"{_MARKER} missing from .gitignore - _commit_wip's `git add -A` will "
        "sweep it into story commits (Mode 17)"
    )


def test_marker_is_in_worktree_log_excludes():
    """The marker must be written into every worktree's .git/info/exclude."""
    assert _MARKER in paths._WORKTREE_LOG_EXCLUDES, (
        f"{_MARKER} missing from pipeline.paths._WORKTREE_LOG_EXCLUDES - "
        "dispatched worktrees of other repos will not ignore it"
    )


def test_gitignore_entry_keeps_marker_out_of_git_add_all(tmp_path):
    """End-to-end: with the repo's .gitignore in place, `git add -A` must not
    stage the marker."""
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text((_REPO_ROOT / ".gitignore").read_text())
    (repo / _MARKER).write_text("ok\n")
    (repo / "real_work.py").write_text("x = 1\n")

    _git("add", "-A", cwd=repo)
    staged = _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()

    assert "real_work.py" in staged
    assert _MARKER not in staged
    assert _MARKER not in _git("status", "--porcelain", cwd=repo).stdout


def test_info_exclude_keeps_marker_out_of_git_add_all(tmp_path):
    """End-to-end: `_exclude_worktree_logs_from_tracking` alone (no
    .gitignore) must keep the marker out of `git add -A`."""
    repo = _init_repo(tmp_path / "repo")
    paths._exclude_worktree_logs_from_tracking(repo)
    (repo / _MARKER).write_text("ok\n")
    (repo / "real_work.py").write_text("x = 1\n")

    _git("add", "-A", cwd=repo)
    staged = _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()

    assert "real_work.py" in staged
    assert _MARKER not in staged
    assert _MARKER not in _git("status", "--porcelain", cwd=repo).stdout


@pytest.mark.parametrize("name", [_MARKER])
def test_marker_name_is_the_one_dispatch_writes(name):
    """Pin the literal: the exclusion lists and the writer must agree."""
    import inspect

    from pipeline import dispatch

    src = inspect.getsource(dispatch)
    assert f'"{name}"' in src or f"'{name}'" in src
