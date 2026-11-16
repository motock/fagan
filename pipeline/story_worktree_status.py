"""Read-only worktree status for one story (branch, commits ahead, dirty paths).

The worktree path comes from the story record and is contain-checked under the
worktrees root by ``worktree_diff.resolve_worktree``: callers never pass a path,
so a manifest cannot name a directory outside the root.

Every git call is a fixed argv list run with ``cwd=<worktree>``, no shell, a
short timeout, ``GIT_OPTIONAL_LOCKS=0`` and ``-c core.fsmonitor=false``, so
collecting status never writes to the worktree, never takes the index lock and
never starts a filesystem monitor. A path read out of porcelain output is
returned as data and never joined to anything -- a filename is an untrusted
string even when git produced it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from pipeline.story_evidence import redact
from pipeline.worktree_diff import resolve_worktree

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 10
MAX_COMMITS_AHEAD = 10
MAX_UNCOMMITTED = 200
MAX_SCRATCHPAD_CHARS = 20000
# Filesystem PATH_MAX; no real repo-relative path is longer.
MAX_PATH_CHARS = 4096

SCRATCHPAD_NAME = ".agent_scratchpad.md"

# Same candidate order as the diff summary: the first default branch this
# worktree shares history with is the base "ahead" is measured against.
_BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


def _unavailable(reason: str) -> dict[str, Any]:
    """The stable body every deny/error path returns."""
    return {
        "available": False,
        "reason": reason,
        "branch": None,
        "head": None,
        "commits_ahead": [],
        "uncommitted": [],
        "scratchpad": "",
    }


def _git(worktree: Path, args: list[str]) -> str | None:
    """Run a read-only git command; None on a non-zero exit, timeout or OSError.

    Neither stdout nor stderr is logged: both can carry paths and file content
    out of the worktree, and the caller only needs to know the call failed.
    """
    cmd = [
        "git", "--no-pager", "-c", "core.quotepath=false",
        "-c", "core.fsmonitor=false", *args,
    ]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            cmd,
            check=False,
            cwd=worktree,
            shell=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("story worktree git call failed", extra={"git_subcommand": args[0]})
        return None
    if result.returncode != 0:
        logger.warning(
            "story worktree git call exited non-zero",
            extra={"git_subcommand": args[0], "returncode": result.returncode},
        )
        return None
    return result.stdout


def _base_sha(worktree: Path) -> str | None:
    """Sha of the default-branch commit this worktree's HEAD descends from."""
    for candidate in _BASE_CANDIDATES:
        out = _git(worktree, ["merge-base", "HEAD", candidate])
        if out and out.strip():
            return out.strip()
    return None


def _commits_ahead(worktree: Path, base: str | None, head: str) -> list[dict[str, str]] | None:
    """Up to MAX_COMMITS_AHEAD newest commits between *base* and *head*.

    None means git failed; an empty list means the branch is level with its base
    or no base could be resolved.
    """
    if base is None:
        return []
    out = _git(
        worktree,
        ["log", f"--max-count={MAX_COMMITS_AHEAD}", "--format=%H%x1f%s", f"{base}..{head}"],
    )
    if out is None:
        return None
    commits: list[dict[str, str]] = []
    for line in out.splitlines():
        if not line:
            continue
        sha, _, subject = line.partition("\x1f")
        commits.append({"sha": sha, "subject": subject})
    return commits


def _parse_status(raw: str) -> list[dict[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into {path, state} records.

    ``-z`` means NUL-separated and never C-quoted, so a path with a space, a
    newline or a leading dash arrives intact. A rename or copy record is
    followed by the original path as its own token; it is consumed so the
    records stay aligned, and it is never used to build a path.
    """
    records: list[dict[str, str]] = []
    tokens = raw.split("\0")
    index = 0
    while index < len(tokens) and len(records) < MAX_UNCOMMITTED:
        token = tokens[index]
        index += 1
        if len(token) < 3:
            continue
        state = token[:2].strip() or "??"
        if token[0] in ("R", "C"):
            index += 1
        records.append({"path": token[3:][:MAX_PATH_CHARS], "state": state})
    return records


def _scratchpad(worktree: Path) -> str:
    """Redacted, length-capped scratchpad text; empty when absent or refused."""
    candidate = worktree / SCRATCHPAD_NAME
    try:
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(worktree) or not resolved.is_file():
            return ""
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_SCRATCHPAD_CHARS + 1)
    except OSError:
        return ""
    # Mask after truncating: masking first and cutting afterwards could split a
    # redaction marker or a token at the boundary.
    return redact(text[:MAX_SCRATCHPAD_CHARS])


def collect(story: dict[str, Any], worktree_root: Path) -> dict[str, Any]:
    """Status of the story's worktree, or an explicit denial.

    A story whose ``worktree`` is missing, relative, absent from disk or
    outside *worktree_root* reports ``{"available": False, "reason":
    "no_worktree"}``; a git failure or timeout reports ``"git_failed"``. Both
    carry the full empty body so a caller reads one shape either way.
    """
    worktree = resolve_worktree(story, worktree_root)
    if worktree is None:
        return _unavailable("no_worktree")
    branch = _git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = _git(worktree, ["rev-parse", "HEAD"])
    status = _git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
    if branch is None or head is None or status is None:
        return _unavailable("git_failed")
    head = head.strip()
    commits = _commits_ahead(worktree, _base_sha(worktree), head)
    if commits is None:
        return _unavailable("git_failed")
    return {
        "available": True,
        "branch": branch.strip() or None,
        "head": head or None,
        "commits_ahead": commits,
        "uncommitted": _parse_status(status),
        "scratchpad": _scratchpad(worktree),
    }
