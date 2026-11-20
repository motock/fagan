"""Diff evidence for the overlord's high-risk merge ruling (coa-06).

The overlord rules on a high-risk merge from the verdicts, PR checks, risk and
local checks, none of which show the change itself. This module renders the
``DIFF EVIDENCE:`` block that puts the change in front of it: the diff stat,
the story's declared ``files``, the changed files outside them, the existing
tests modified or deleted, and a redacted production diff excerpt.

Every git call is read-only and runs in the story's worktree. Any failure
(no worktree, a non-zero exit, a timeout or an OSError) renders the single line
``DIFF EVIDENCE: unavailable``: missing evidence, never a pass, and never an
exception that would stop the adjudication.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import PurePosixPath
from typing import Any

from .story_evidence import redact

#: The most characters of the redacted production diff the overlord sees.
DIFF_EXCERPT_MAX_CHARS = 12000

#: The most file lines of the diff stat shown; the summary line always follows.
DIFF_STAT_MAX_FILES = 40

GIT_TIMEOUT_SECONDS = 30

UNAVAILABLE = "DIFF EVIDENCE: unavailable"

_DIFF_RANGE = "origin/HEAD...HEAD"
_GIT_FLAGS = ("--no-ext-diff", "--no-color")
_TEST_DIRECTORIES = frozenset({"tests", "test"})
_TEST_BASENAMES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def is_test_file(path: str) -> bool:
    """True when *path* is a test: a ``tests/`` or ``test/`` directory or a test basename."""
    parts = PurePosixPath(path).parts
    if any(part in _TEST_DIRECTORIES for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _TEST_BASENAMES)


def _git_diff(story: dict[str, Any], *args: str) -> str | None:
    """Output of ``git diff`` in the story's worktree, or None when it cannot be read."""
    worktree = story.get("worktree")
    if not worktree:
        return None
    try:
        proc = subprocess.run(
            ["git", "diff", *_GIT_FLAGS, *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _stat_lines(stat: str) -> list[str]:
    lines = stat.splitlines()
    if len(lines) > DIFF_STAT_MAX_FILES:
        lines = lines[:DIFF_STAT_MAX_FILES] + [lines[-1]]
    return lines


def _modified_or_deleted_tests(name_status: str) -> list[str]:
    """Test paths that git reports as modified (M) or deleted (D)."""
    paths = []
    for line in name_status.splitlines():
        status, _, path = line.partition("\t")
        if status in ("M", "D") and is_test_file(path):
            paths.append(path)
    return paths


def _joined(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


def render_diff_evidence(story: dict[str, Any]) -> str:
    """The ``DIFF EVIDENCE:`` block for *story*, or ``DIFF EVIDENCE: unavailable``."""
    stat = _git_diff(story, "--stat", _DIFF_RANGE)
    names = _git_diff(story, "--name-only", _DIFF_RANGE)
    name_status = _git_diff(story, "--name-status", "--diff-filter=MD", _DIFF_RANGE)
    if stat is None or names is None or name_status is None:
        return UNAVAILABLE

    declared = list(story.get("files") or [])
    changed = [path for path in names.splitlines() if path]
    outside = [path for path in changed if path not in declared]
    production = [path for path in changed if not is_test_file(path)]
    # An empty pathspec would make ``git diff`` print the test files too.
    diff = _git_diff(story, _DIFF_RANGE, "--", *production) if production else ""
    if diff is None:
        return UNAVAILABLE

    redacted = redact(diff)
    excerpt = redacted[:DIFF_EXCERPT_MAX_CHARS]
    modified_tests = _joined(_modified_or_deleted_tests(name_status))
    lines = [
        "DIFF EVIDENCE:",
        "diff stat:",
        *(_stat_lines(stat) or ["(none)"]),
        f"declared files: {_joined(declared)}",
        f"outside declared files: {_joined(outside)}",
        f"existing tests modified or deleted: {modified_tests}",
        "production diff (redacted, first 12000 chars):",
        excerpt or "(none)",
    ]
    if len(redacted) > DIFF_EXCERPT_MAX_CHARS:
        lines.append("[truncated]")
    return "\n".join(lines)
