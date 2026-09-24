"""Graded oracle: the fresh-dispatch command-shape tests must stay green while
another process holds the advisory git lock.

Both graded tests assert that a fresh dispatch ran ``git fetch origin <default>``,
but that fetch is guarded by a non-blocking advisory lock on
``<repo_root>/.git/.pipeline-git-lock`` (``pipeline/dispatch_worktree.py``). A
test manifest without a ``repo_root`` makes ``_repo_root_for`` fall back to the
global ``REPO_ROOT``, so every xdist worker contends on one lock file: while any
other worker holds it the fetch is legitimately skipped and the assertion fails.
Observed on CI 2026-09-24, where only one Python version lost the race.

The graded tests must therefore pin the lock as acquired (they assert a command
sequence, not lock arbitration). This fixture proves the pin is really there by
holding the lock and running them in a subprocess.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GRADED_TESTS = [
    (
        "tests/unit/test_pipeline_mcp_server_advance_orchestration_1.py"
        "::test_dispatch_story_fresh_creates_worktree_and_dispatches"
    ),
    (
        "tests/unit/test_dispatch_worktree_from_origin.py"
        "::test_dispatch_git_commands_are_fetch_and_worktree_add_from_origin_no_pull"
    ),
]


@contextlib.contextmanager
def _held_git_lock(repo_root):
    """Hold the advisory git lock from this process, as a concurrent worker
    would. flock contention is per open-file-description, so the pytest
    subprocess below (a separate process) sees the lock as held."""
    lock_path = repo_root / ".git" / ".pipeline-git-lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock_path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_graded_command_shape_tests_still_fetch_while_the_lock_is_held(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    with _held_git_lock(repo):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider",
             *GRADED_TESTS],
            cwd=REPO_ROOT,
            env={**os.environ, "REPO_ROOT": str(repo)},
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, (
        "a fresh dispatch must still fetch while another process holds the "
        "advisory git lock; unpinned, the fetch is skipped and the graded "
        "assertion fails:\n" + result.stdout[-3000:]
    )
