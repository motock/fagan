"""Regression guard: the local-agent bash tool must refuse `git stash`.

The git stash list is shared by every worktree of a repo, so one agent's
`git stash`/`git stash pop` can deliver a foreign change into another
story's worktree (observed 2026-11-04 on NTF-01). Read-only inspection
(`git stash list`, `git stash show`) stays allowed.
"""

import pytest

from pipeline.local_agent_common import destructive_git_op


@pytest.mark.parametrize(
    "cmd",
    [
        "git stash",
        "git stash pop",
        "git stash push -m x",
        "git status && git stash",
    ],
)
def test_git_stash_is_refused(cmd):
    assert destructive_git_op(cmd) == "git stash"


@pytest.mark.parametrize(
    "cmd",
    [
        "git stash list",
        "git stash show -p",
        "git status",
        "echo stash",
    ],
)
def test_non_stash_write_commands_are_allowed(cmd):
    assert destructive_git_op(cmd) is None


def test_git_reset_hard_still_detected():
    assert destructive_git_op("git reset --hard") == "git reset --hard"
