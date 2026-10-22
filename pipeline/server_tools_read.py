"""Read-only MCP tools. Plain functions, registered by pipeline.server (same
pattern as server_tools_lifecycle); _service resolves through pipeline.server
at call time so tests patching pipeline.server._service keep landing."""

from typing import Any

from .module_ref import _ModuleRef

_service = _ModuleRef("pipeline.server", "_service")

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
