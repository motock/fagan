"""Read-only diff summary for a story's worktree (dashboard drill-down).

Every git call is an argv list run with cwd=<worktree>, no shell, a short
timeout and GIT_OPTIONAL_LOCKS=0, so collecting a summary never writes to the
worktree and never takes the index lock.
"""

import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from pipeline.paths import _WORKTREE_LOG_EXCLUDES

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 10
_BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")
_UNTRACKED_READ_LIMIT = 1_000_000
_BINARY_SNIFF_BYTES = 8000
_UNTRACKED_DIFF_LIMIT = 1024 * 1024

# Anchored with "**/" so a bare name like "agent.log" matches at any depth,
# independent of whether .git/info/exclude was ever written for this repo.
_EXCLUDE_PATHSPECS = tuple(f":(exclude,glob)**/{pattern}" for pattern in _WORKTREE_LOG_EXCLUDES)


def _git(worktree: Path, args: list[str], ok_returncodes: tuple[int, ...] = (0,)) -> str | None:
    """Run a read-only git command; None on a non-ok exit, timeout or OSError."""
    cmd = ["git", "--no-pager", "-c", "core.quotepath=false", *args]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            cmd, check=False, cwd=worktree, shell=False, timeout=_GIT_TIMEOUT_SECONDS, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("worktree_diff git call failed", extra={"git_subcommand": args[0]})
        return None
    return result.stdout if result.returncode in ok_returncodes else None


def resolve_worktree(story: dict[str, Any], worktree_root: Path) -> Path | None:
    """The story's worktree if it is absolute and contained in worktree_root."""
    if not isinstance(story, dict):
        return None
    raw_wt = story.get("worktree")
    if not isinstance(raw_wt, str) or not raw_wt:
        return None
    wt_path = Path(raw_wt)
    if not wt_path.is_absolute():
        return None
    try:
        resolved = wt_path.resolve(strict=False)
        root = worktree_root.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            return None
        if not resolved.is_dir():
            return None
    except OSError:
        return None
    return resolved


def resolve_base_sha(worktree: Path) -> tuple[str | None, str | None]:
    """(sha, ref) of the first default-branch candidate sharing history with HEAD."""
    for candidate in _BASE_CANDIDATES:
        out = _git(worktree, ["merge-base", "HEAD", candidate])
        if out and out.strip():
            return out.strip(), candidate
    return None, None


def _unavailable(reason: str, **fields: Any) -> dict[str, Any]:
    return {
        "available": False, "reason": reason, "branch": None, "head_sha": None,
        "base_ref": None, "base_sha": None, "files": [],
        "totals": {"files": 0, "added": 0, "removed": 0, "untracked": 0, "committed": 0, "working": 0},
        "truncated": False, **fields,
    }


def _parse_name_status(raw: str) -> dict[str, tuple[str, str | None]]:
    """path -> (status letter, old path for renames/copies)."""
    tokens = raw.split("\0")
    out: dict[str, tuple[str, str | None]] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i][0]
        if status in ("R", "C") and i + 2 < len(tokens):
            out[tokens[i + 2]] = (status, tokens[i + 1])
            i += 3
        elif i + 1 < len(tokens):
            out[tokens[i + 1]] = (status, None)
            i += 2
        else:
            break
    return out


def _parse_numstat(raw: str) -> dict[str, tuple[int, int, bool]]:
    """path -> (added, removed, binary)."""
    tokens = raw.split("\0")
    out: dict[str, tuple[int, int, bool]] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        added, removed, path = tokens[i].split("\t", 2)
        if path == "" and i + 2 < len(tokens):  # rename: empty path, then old, new
            path = tokens[i + 2]
            i += 3
        else:
            i += 1
        is_binary = added == "-"
        out[path] = (0 if is_binary else int(added), 0 if is_binary else int(removed), is_binary)
    return out


def _count_untracked(worktree: Path, rel_path: str) -> tuple[int, bool]:
    """(line count, is_binary) for an untracked file, read without writing."""
    try:
        with open(worktree / rel_path, "rb") as handle:
            data = handle.read(_UNTRACKED_READ_LIMIT)
    except OSError:
        return 0, False
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return 0, True
    lines = data.count(b"\n")
    if data and not data.endswith(b"\n"):
        lines += 1
    return lines, False


def _paths(raw: str | None) -> list[str]:
    return [p for p in (raw or "").split("\0") if p]


def collect_diff_summary(
    worktree: Path | None, base_sha: str | None, max_files: int = 500, base_ref: str | None = None,
) -> dict[str, Any]:
    """Summarise base_sha..working-tree changes (committed, staged, unstaged, untracked)."""
    if worktree is None:
        return _unavailable("invalid_worktree")
    if not worktree.is_dir():
        return _unavailable("worktree_gone")
    if not (worktree / ".git").exists():
        return _unavailable("not_a_worktree")
    if not base_sha:
        return _unavailable("no_base_ref")

    head_sha = _git(worktree, ["rev-parse", "HEAD"])
    if head_sha is None:
        return _unavailable("not_a_worktree")
    branch = (_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"]) or "").strip() or None

    diff_args = ["--no-ext-diff", "--no-textconv", "--no-color", "-M"]
    tail = ["--", ".", *_EXCLUDE_PATHSPECS]
    name_status = _git(worktree, ["diff", *diff_args, "--name-status", "-z", base_sha, *tail])
    numstat = _git(worktree, ["diff", *diff_args, "--numstat", "-z", base_sha, *tail])
    if name_status is None or numstat is None:
        return _unavailable("git_failed", branch=branch, head_sha=head_sha.strip())
    committed = _git(worktree, ["diff", *diff_args, "--name-only", "-z", base_sha, "HEAD", *tail])
    working = _git(worktree, ["diff", *diff_args, "--name-only", "-z", "HEAD", *tail])
    untracked_raw = _git(worktree, ["ls-files", "-z", "--others", "--exclude-standard", *tail])

    statuses = _parse_name_status(name_status)
    counts = _parse_numstat(numstat)
    entries: list[dict[str, Any]] = []
    for path, (status, old_path) in statuses.items():
        added, removed, is_binary = counts.get(path, (0, 0, False))
        entry = {"path": path, "status": status, "added": added, "removed": removed,
                 "binary": is_binary, "untracked": False}
        if old_path is not None:
            entry["old_path"] = old_path
        entries.append(entry)
    untracked_paths = _paths(untracked_raw)
    entries.extend(
        {"path": path, "status": "?", "added": added, "removed": 0, "binary": is_binary, "untracked": True}
        for path in untracked_paths
        for added, is_binary in [_count_untracked(worktree, path)]
    )
    entries.sort(key=lambda e: e["path"])
    truncated = len(entries) > max_files
    entries = entries[:max_files]

    return {
        "available": True, "reason": None, "branch": branch, "head_sha": head_sha.strip(),
        "base_ref": base_ref, "base_sha": base_sha, "files": entries,
        "totals": {
            "files": len(entries),
            "added": sum(e["added"] for e in entries),
            "removed": sum(e["removed"] for e in entries),
            "untracked": sum(1 for e in entries if e["untracked"]),
            "committed": len(_paths(committed)),
            "working": len(_paths(working)) + len(untracked_paths),
        },
        "truncated": truncated,
    }


def _is_safe_relative_path(path: Any, allowed_paths: set[str]) -> bool:
    if not isinstance(path, str) or not path or path not in allowed_paths:
        return False
    if path.startswith("/") or "\\" in path or "\0" in path:
        return False
    parts = path.split("/")
    return ".." not in parts and parts[0] != ".git"


def _file_result(path: str, diff: str = "", **fields: Any) -> dict[str, Any]:
    return {"available": True, "path": path, "diff": diff, "truncated": False, "binary": False, **fields}


def _untracked_skip_reason(worktree: Path, path: str) -> str | None:
    """Why an untracked file must not be read, or None when it is safe to diff."""
    full = worktree / path
    try:
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode):
            return "symlink"
        if not stat.S_ISREG(info.st_mode):
            return "not_regular"
        if not full.resolve().is_relative_to(worktree.resolve()):
            return "outside_worktree"
    except OSError:
        return "unreadable"
    return "too_large" if info.st_size > _UNTRACKED_DIFF_LIMIT else None


def collect_file_diff(
    worktree: Path, base_sha: str, path: str, allowed_paths: set[str], max_chars: int = 40000,
) -> dict[str, Any]:
    """Unified diff of one file from the summary's file list; never raises."""
    if not _is_safe_relative_path(path, allowed_paths):
        return {"ok": False, "status": 400, "error": "invalid path"}
    if not base_sha:
        return {"available": False, "reason": "no_base_ref"}

    is_untracked = bool(_git(worktree, ["ls-files", "-z", "--others", "--exclude-standard", "--", path]))
    if is_untracked:
        skipped = _untracked_skip_reason(worktree, path)
        if skipped:
            return _file_result(path, skipped=skipped)
        diff_args = ["diff", "--no-index", "--no-ext-diff", "--no-textconv", "--no-color"]
        target = ["--", "/dev/null", path]
        ok_codes = (0, 1)  # --no-index exits 1 when the files differ
    else:
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        target = [base_sha, "--", path]
        ok_codes = (0,)

    numstat = _git(worktree, [*diff_args, "--numstat", *target], ok_codes)
    if numstat is None:
        return {"available": False, "reason": "git_failed"}
    if numstat.startswith("-\t-\t"):
        return _file_result(path, binary=True)
    diff = _git(worktree, [*diff_args, *target], ok_codes)
    if diff is None:
        return {"available": False, "reason": "git_failed"}
    if len(diff) > max_chars:
        return _file_result(
            path, diff[:max_chars], truncated=True,
            note=f"diff truncated to the first {max_chars} of {len(diff)} characters",
        )
    return _file_result(path, diff)
