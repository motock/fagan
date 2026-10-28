"""PipelineService.set_story_status and get_story_diff bodies, moved out of
pipeline/service.py (line-count target). The methods keep their signatures and
delegate here. Server-owned names are _ServerRef bindings, exactly as in
pipeline.service, so tests patching pipeline.server still land.
"""

from typing import Any

from pipeline import worktree_diff


def _set_story_status_impl(self, plan_name: str, story_key: str, status: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    if status not in _VALID_STORY_STATUSES:
        return {
            "ok": False,
            "error": f"invalid status {status!r}: "
            f"must be one of {sorted(_VALID_STORY_STATUSES)}",
        }

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        story["status"] = status
        if status != "parked":
            story.pop("parked_reason", None)
        _store.save_manifest(plan_name, manifest)
        return {"ok": True, "story_key": story_key, "status": status}


def _get_story_diff_impl(
    self,
    plan_name: str,
    story_key: str,
    manifest: dict[str, Any],
    path: str | None = None,
) -> dict[str, Any]:
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    story = stories.get(story_key) if isinstance(stories, dict) else None
    if not isinstance(story, dict):
        return {"ok": False, "status": 404, "error": "unknown plan/story"}
    worktree = worktree_diff.resolve_worktree(story, WORKTREE_ROOT._value())
    if worktree is None:
        return {"available": False, "reason": "no_worktree"}
    base_sha, base_ref = worktree_diff.resolve_base_sha(worktree)
    summary = worktree_diff.collect_diff_summary(worktree, base_sha, base_ref=base_ref)
    if path is None or not summary["available"]:
        return summary
    allowed_paths = {entry["path"] for entry in summary["files"]}
    return worktree_diff.collect_file_diff(worktree, base_sha, path, allowed_paths)


# must follow the defs: service.py back-imports them, so defining them first
# keeps both import directions cycle-free
from .service import _ServerRef

_VALID_STORY_STATUSES = _ServerRef("_VALID_STORY_STATUSES")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
