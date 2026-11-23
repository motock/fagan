"""Read-only MCP tools. Plain functions, registered by pipeline.server (same
pattern as server_tools_lifecycle); _service resolves through pipeline.server
at call time so tests patching pipeline.server._service keep landing."""

from typing import Any

from . import repo_listing_hooks, story_worktree_status
from .module_ref import _ModuleRef
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


TOOLS = (list_repositories,)
