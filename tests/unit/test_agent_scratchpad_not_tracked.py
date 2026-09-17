"""Regression tests: .agent_scratchpad.md must never be tracked by git.

``.agent_scratchpad.md`` is per-story agent scratch state. The repo
``.gitignore`` excludes it (``.agent_scratchpad*.md``) with a comment stating it
must never be part of the source diff, and ``pipeline/paths.py``'s
``_WORKTREE_LOG_EXCLUDES`` lists it for the same reason: tracking it previously
caused spurious merge-gate rebase conflicts between unrelated concurrent
stories (a rework's ``git add -A`` WIP-commit swept the shared scratch path
into story commits).

Regression: this branch committed ``.agent_scratchpad.md`` (blob ``d8050ac``,
added in ``afdc682``) even though it is absent from the base commit and both
exclusion lists name it, so merging re-introduces that conflict regression.

These tests pin the fix (untrack the file and drop the scratchpad-only
commits):

  1. ``git ls-files`` does not list ``.agent_scratchpad.md``.
  2. HEAD's tree does not contain ``.agent_scratchpad.md`` (catches a
     ``git rm --cached`` that was staged but never committed).
  3. ``git check-ignore`` reports the path as ignored - an ignore rule only
     takes effect once the file is untracked.
  4. The ``.gitignore`` rule and ``pipeline/paths.py``'s
     ``_WORKTREE_LOG_EXCLUDES`` entry survive the fix (they must not be
     removed to make the file "clean").

RED until the fix lands: tests 1-3 fail while the file is tracked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.paths import _WORKTREE_LOG_EXCLUDES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = ".agent_scratchpad.md"
GITIGNORE = REPO_ROOT / ".gitignore"


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git in the repo root, capturing output without raising."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_scratchpad_is_not_tracked_by_git():
    """``git ls-files`` must not list .agent_scratchpad.md."""
    result = _git("ls-files", "--", SCRATCHPAD)
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"{SCRATCHPAD} is tracked by git (git ls-files lists it). It is agent "
        "scratch state that .gitignore and pipeline/paths.py's "
        "_WORKTREE_LOG_EXCLUDES both exclude; tracking it re-introduces the "
        "spurious merge-gate rebase conflicts between concurrent stories. "
        f"Fix: git rm --cached {SCRATCHPAD}"
    )


def test_scratchpad_is_absent_from_head_tree():
    """HEAD's tree must not carry .agent_scratchpad.md."""
    result = _git("cat-file", "-e", f"HEAD:{SCRATCHPAD}")
    assert result.returncode != 0, (
        f"{SCRATCHPAD} is present in the HEAD tree; it must not be committed "
        "(a staged-but-uncommitted `git rm --cached` is not enough)."
    )


def test_scratchpad_is_ignored_by_git():
    """``git check-ignore`` must report the path as ignored.

    An ignore rule only takes effect once the file is untracked, so this is
    red while the file is tracked and green after the fix.
    """
    result = _git("check-ignore", "--", SCRATCHPAD)
    assert result.returncode == 0, (
        f"{SCRATCHPAD} is not reported as ignored by git check-ignore "
        f"(exit {result.returncode}). .gitignore's '.agent_scratchpad*.md' "
        "rule only takes effect once the file is untracked."
    )


def test_gitignore_still_excludes_scratchpad():
    """The .gitignore exclusion must survive the fix."""
    rules = [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert ".agent_scratchpad*.md" in rules, (
        ".gitignore must keep the '.agent_scratchpad*.md' exclusion - it must "
        "not be removed to make the tracked file 'clean'."
    )


def test_worktree_log_excludes_still_lists_scratchpad():
    """pipeline/paths.py's exclusion list must survive the fix."""
    assert ".agent_scratchpad.md" in _WORKTREE_LOG_EXCLUDES, (
        "pipeline/paths.py's _WORKTREE_LOG_EXCLUDES must keep '.agent_scratchpad.md'"
    )
    assert ".agent_scratchpad*.md" in _WORKTREE_LOG_EXCLUDES, (
        "pipeline/paths.py's _WORKTREE_LOG_EXCLUDES must keep '.agent_scratchpad*.md'"
    )
