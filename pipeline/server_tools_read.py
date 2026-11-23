"""Read-only MCP tools. Plain functions, registered by pipeline.server (same
pattern as server_tools_lifecycle); _service resolves through pipeline.server
at call time so tests patching pipeline.server._service keep landing."""

import logging
import os
from collections import deque
from collections.abc import Callable
from typing import Any

from . import decision_record_tool, repo_listing_hooks, story_worktree_status
from .actor import current_correlation_id, current_principal
from .audit import append_host_audit
from .module_ref import _ModuleRef
from .repo_read_access import RepoReadRefused, RepoUrlNotAllowed, read_root_kwargs
from .story_evidence import (  # noqa: F401  (get_story_events is re-exported)
    REDACTED,
    get_story_events,
    is_secret_name,
    redact,
)
from .workspace_reader import STORY_LOG_TAIL_DEFAULT, STORY_LOG_TAIL_MAX

_service = _ModuleRef("pipeline.server", "_service")
_worktree_root = _ModuleRef("pipeline.server", "WORKTREE_ROOT")
_store = _ModuleRef("pipeline.server", "_store")

# Filesystem PATH_MAX; no real repo-relative path is longer.
MAX_PATH_LENGTH = 4096


def get_story_diff(plan_name: str, story_key: str, path: str | None = None) -> dict[str, Any]:
    """
    Return the in-flight story's worktree diff summary, or one file's diff
    when `path` is given.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.
    path: optional repo-relative path; must be one of the files listed in the
        summary. Over-long values are truncated to MAX_PATH_LENGTH and so
        will not match any listed file.

    A story with no worktree returns {"available": False, "reason": ...}
    rather than raising. An unknown plan/story returns
    {"ok": False, "status": 404, "error": ...}.
    """
    if path is not None:
        path = path[:MAX_PATH_LENGTH]
    manifest = _service.get_manifest_or_none(plan_name) or {}
    return _service.get_story_diff(plan_name, story_key, manifest, path=path)


_NOT_FOUND = {"ok": False, "status": 404, "error": "unknown plan/story"}


def _known_story(plan_name: str, story_key: str) -> dict[str, Any] | None:
    manifest = _service.get_manifest_or_none(plan_name)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict) or story_key not in stories:
        return None
    return manifest


def _redact_value(value: Any) -> Any:
    """Return a masked copy; never mutate the store's own lists or dicts."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(item, str) and is_secret_name(key)
            else _redact_value(item)
            for key, item in value.items()
        }
    return value


def get_story_log(plan_name: str, story_key: str, lines: int = STORY_LOG_TAIL_DEFAULT) -> dict[str, Any]:
    """
    Return the tail of the story's log with secret values masked.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.
    lines: tail length, clamped to 1..500; a non-integer uses the default.

    A story with no readable log returns {"available": False, "lines": []}.
    An unknown plan/story returns {"ok": False, "status": 404, "error": ...}.
    """
    manifest = _known_story(plan_name, story_key)
    if manifest is None:
        return dict(_NOT_FOUND)
    if not isinstance(lines, int):
        lines = STORY_LOG_TAIL_DEFAULT
    lines = max(1, min(lines, STORY_LOG_TAIL_MAX))
    result = _service.get_story_log(plan_name, story_key, manifest, lines)
    # Redact the joined text so a multi-line PEM block is masked whole.
    masked = redact("\n".join(result.get("lines", []))) if result.get("lines") else ""
    return {**result, "lines": masked.split("\n") if masked else []}


def get_story_worktree(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Return the story's worktree status: branch, head, commits ahead of the
    default branch (max 10), uncommitted paths (max 200) and the agent's
    scratchpad with secret values masked (max 20000 characters).

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.

    The worktree is resolved from the story record and contain-checked under
    the worktrees root; there is no path argument. A story with no worktree
    returns {"available": False, "reason": "no_worktree"}, a git failure
    {"available": False, "reason": "git_failed"}. An unknown plan/story
    returns {"ok": False, "status": 404, "error": ...}.
    """
    manifest = _known_story(plan_name, story_key)
    if manifest is None:
        return dict(_NOT_FOUND)
    return story_worktree_status.collect(
        manifest["stories"][story_key], _worktree_root._value()
    )


def get_story_journal(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Return the story's checkpoint journal with secret values masked.

    Returns {"available": bool, "entries": [...]}; a story that has not
    checkpointed is {"available": False, "entries": []}. An unknown
    plan/story returns {"ok": False, "status": 404, "error": ...}.
    """
    if _known_story(plan_name, story_key) is None:
        return dict(_NOT_FOUND)
    available, entries = _service.get_journal(plan_name, story_key)
    return {"available": available, "entries": _redact_value(entries)}


def list_repositories() -> dict[str, Any]:
    """
    List the repositories the caller may work with, in its namespace. No
    parameters: the answer is always the namespace bound to this call.

    Returns {"ok": true, "repositories": [{"repo": "host/owner/name",
    "level": ..., "state": ...}]} sorted by repo, at most 500 items
    ("truncated": true when cut). Without a deployment listing plugin the
    repositories are those of the plans list_plans shows you, and level and
    state are null. A failure returns {"ok": false, "reason":
    "repositories_unavailable", "correlation_id": ...}.
    """
    return repo_listing_hooks.list_repositories_result(_store._value())


# --- repository reads -------------------------------------------------------
#
# The three tools below serve source from a managed clone. The order of each
# call is fixed: validate the identifier, rate-limit the principal, ask the
# access hook (through the shared resolver) before any clone is named, answer
# a missing clone without touching git, then read. One host audit record per
# call names the tool, the repository and the outcome, never a path, a pattern
# or content.

logger = logging.getLogger("pipeline")

REPO_READ_RATE_ENV = "PIPELINE_REPO_READ_PER_MINUTE"
REPO_READ_DEFAULT_PER_MINUTE = 120
REPO_READ_ACTION = "repo_read"
REFUSED_REASON = "repository_access_refused"
NOT_AVAILABLE_REASON = "repository_not_available"
INVALID_REPOSITORY_REASON = "invalid_repository"
RATE_LIMITED_REASON = "rate_limited"

#: The rolling window of repository reads, per principal, in process memory.
#: The limiter is ``record_decision``'s; the window and the limit are this tool's own.
_READ_WINDOWS: dict[str, deque[float]] = {}

#: The generic errors the workspace service returns, as stable reason tokens.
#: Anything else is ``read_failed``, so no service text reaches the caller.
_SERVICE_REASONS = {
    "invalid path": "invalid_path",
    "not found": "not_found",
    "not a directory": "not_a_directory",
    "file too large": "file_too_large",
    "not a text file": "not_a_text_file",
    "invalid pattern": "invalid_pattern",
    "search timed out": "search_timed_out",
    "search failed": "search_failed",
    "repository not cloned yet": NOT_AVAILABLE_REASON,
}


def _valid_repository(repo: Any) -> bool:
    """Return whether ``repo`` is exactly ``host/owner/name``, as listed."""
    return (
        isinstance(repo, str)
        and ".." not in repo
        and repo_listing_hooks._REPO_RE.fullmatch(repo) is not None
    )


def _audit_read(tool: str, repository: str | None, outcome: str) -> None:
    """Write the one host audit record for one call; ``None`` names no repository."""
    try:
        append_host_audit(
            action=REPO_READ_ACTION,
            outcome=outcome,
            target={"repository": repository},
            detail={"tool": tool, "correlation_id": current_correlation_id()},
        )
    except Exception:  # noqa: BLE001 - a failed audit append never changes the answer
        logger.warning("repository read audit append failed", extra={"tool": tool})


def _repository_read(
    tool: str, repo: Any, read: Callable[[str], dict[str, Any]]
) -> dict[str, Any]:
    """Run one repository read through the gate, in the fixed order."""
    if not _valid_repository(repo):
        _audit_read(tool, None, INVALID_REPOSITORY_REASON)
        return {"ok": False, "reason": INVALID_REPOSITORY_REASON}

    limited = decision_record_tool._limit_refusal(
        current_principal().subject,
        env=REPO_READ_RATE_ENV,
        default=REPO_READ_DEFAULT_PER_MINUTE,
        windows=_READ_WINDOWS,
    )
    if limited is not None:
        _audit_read(tool, repo, RATE_LIMITED_REASON)
        return {
            "ok": False,
            "reason": RATE_LIMITED_REASON,
            "retry_after_seconds": limited["retry_after_seconds"],
        }

    # The shared resolver runs the allow-list, the containment check and the
    # access hook before it names a clone. Any refusal, including a host the
    # allow-list does not know, answers the same way whether or not a clone is
    # present, so the reply is no existence oracle.
    try:
        root = read_root_kwargs(f"https://{repo}")["root"]
    except (RepoUrlNotAllowed, RepoReadRefused):
        _audit_read(tool, repo, REFUSED_REASON)
        return {"ok": False, "reason": REFUSED_REASON}

    if not os.path.isdir(root):
        _audit_read(tool, repo, NOT_AVAILABLE_REASON)
        return {"ok": False, "reason": NOT_AVAILABLE_REASON}

    result = read(root)
    if not result.get("ok"):
        reason = _SERVICE_REASONS.get(result.get("error"), "read_failed")
        _audit_read(tool, repo, reason)
        return {"ok": False, "reason": reason}
    _audit_read(tool, repo, "ok")
    return result


def read_repository_file(repo: str, path: str) -> dict[str, Any]:
    """
    Read one UTF-8 text file from a repository the caller's namespace may read.

    repo: ``host/owner/name``, exactly as list_repositories returns it.
    path: repo-relative path. A path that escapes the clone, including through
        a symlink, is refused; a file over 200000 bytes is refused unread.

    Returns {"ok": true, "path": ..., "content": ...}. A failure returns
    {"ok": false, "reason": ...}: invalid_repository, repository_access_refused,
    repository_not_available, invalid_path, not_found, file_too_large,
    not_a_text_file, rate_limited (with retry_after_seconds) or read_failed.
    """
    return _repository_read(
        "read_repository_file",
        repo,
        lambda root: _service.read_workspace_file(path, root=root),
    )


def list_repository_directory(repo: str, path: str = "") -> dict[str, Any]:
    """
    List the immediate children of a directory in a repository the caller's
    namespace may read. Nothing recurses.

    repo: ``host/owner/name``, exactly as list_repositories returns it.
    path: repo-relative directory; "" names the repository root.

    Returns {"ok": true, "path": ..., "entries": [{"name", "type"}]} with type
    "dir" or "file". Failures return {"ok": false, "reason": ...} with the same
    tokens as read_repository_file, plus not_a_directory.
    """
    return _repository_read(
        "list_repository_directory",
        repo,
        lambda root: _service.list_workspace_directory(path, root=root),
    )


def search_repository(repo: str, pattern: str) -> dict[str, Any]:
    """
    Search a repository the caller's namespace may read for lines that match
    ``pattern`` (grep's basic regular expression), skipping .git and binary files.

    repo: ``host/owner/name``, exactly as list_repositories returns it.
    pattern: the text to search for; it is passed to grep as one argument.

    Returns {"ok": true, "matches": ["path:line:text", ...], "truncated": bool}
    with at most 200 matches. Failures return {"ok": false, "reason": ...} with
    the tokens of read_repository_file, plus invalid_pattern, search_timed_out
    and search_failed.
    """
    return _repository_read(
        "search_repository",
        repo,
        lambda root: _service.search_workspace(pattern, root=root),
    )


TOOLS = (
    list_repositories,
    read_repository_file,
    list_repository_directory,
    search_repository,
)
