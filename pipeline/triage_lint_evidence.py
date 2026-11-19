"""Lint evidence for the ``own_lint_failure`` park class.

``lint_paths`` reads the repo-relative files a linter names in its output.
``branch_changed_paths`` asks git which files the story's branch added or
changed. ``with_lint_evidence`` adds that branch list to a classification
evidence dict, but only when the lint gate is red, so a green story costs no
git call.

Git failures fail closed: an unreadable branch diff is an empty set, so the
``own_lint_failure`` class does not match.
"""

import re
import subprocess
from pathlib import Path

# ruff concise: ``path:line:col: CODE msg``; ruff full: ``  --> path:line:col``.
_LINT_LOCATION_RE = re.compile(r"^[ \t]*(?:-->[ \t]*)?([^\s:]+\.[A-Za-z0-9]+):\d+:\d+", re.MULTILINE)
_GIT_TIMEOUT_SECONDS = 30
_BRANCH_DIFF_COMMAND = ["git", "diff", "--name-only", "--no-renames", "origin/HEAD...HEAD"]


def lint_paths(lint_output: str) -> list[str]:
    """The repo-relative paths named in linter output, unique, in first-seen order."""
    seen: list[str] = []
    for path in _LINT_LOCATION_RE.findall(lint_output):
        if path not in seen:
            seen.append(path)
    return seen


def branch_changed_paths(story: dict) -> frozenset[str]:
    """Paths the story's branch added or changed, read live from its worktree.

    Returns an empty set when the worktree is missing, git fails or times out.
    """
    worktree = story.get("worktree")
    if not isinstance(worktree, str) or not Path(worktree).is_dir():
        return frozenset()
    try:
        result = subprocess.run(
            _BRANCH_DIFF_COMMAND,
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(line for line in result.stdout.splitlines() if line)


def with_lint_evidence(story: dict, evidence: dict) -> dict:
    """A copy of ``evidence`` with ``branch_paths`` when the lint gate is red.

    The branch diff is read only when ``last_lint_check`` carries a non-zero
    return code; otherwise the copy is returned unchanged and git does not run.
    """
    result = dict(evidence)
    lint = story.get("last_lint_check")
    if isinstance(lint, dict) and isinstance(lint.get("returncode"), int) and lint["returncode"] != 0:
        result["branch_paths"] = sorted(branch_changed_paths(story))
    return result
