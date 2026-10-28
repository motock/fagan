"""PipelineService.set_story_status, get_story_diff and patch_story bodies, moved
out of pipeline/service.py (line-count target). The methods keep their signatures
and delegate here. Server-owned names are _ServerRef bindings, exactly as in
pipeline.service, so tests patching pipeline.server still land.
"""

from typing import Any

from pipeline import worktree_diff
from pipeline.plan_approval import approval_enabled, reset_if_changed


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


def _patch_story_impl(
    self, plan_name: str, story_key: str, fields: dict[str, Any]
) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    from .authorization import authorize
    refusal = authorize("story:patch", plan_name=plan_name, story_key=story_key)
    if refusal is not None:
        return refusal

    unknown = set(fields) - _PATCHABLE_STORY_FIELDS
    if unknown:
        return {
            "ok": False,
            "error": f"cannot patch field(s) {sorted(unknown)}: "
            f"only {sorted(_PATCHABLE_STORY_FIELDS)} are editable",
        }

    if "backend" in fields and fields["backend"] not in _VALID_STORY_BACKENDS:
        return {
            "ok": False,
            "error": (
                f"invalid backend {fields['backend']!r}: must be one of "
                f"{sorted(_VALID_STORY_BACKENDS)}"
            ),
        }

    if "blocked_by" in fields and not (
        isinstance(fields["blocked_by"], list)
        and all(isinstance(entry, str) for entry in fields["blocked_by"])
    ):
        return {
            "ok": False,
            "error": "invalid blocked_by: must be a list of "
            '"<plan>:<story_key>" strings',
        }

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        before = _store.get_manifest(plan_name).get("stories", {}).get(story_key)
        previous_instructions = before.get("agent_instructions") if before else None
        was_dispatched = bool(before and before.get("dispatched_at"))
        story = _store.update_story(plan_name, story_key, fields)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        if approval_enabled():
            # Re-read so the digest reflects the edit just persisted, and
            # reset in the same transaction so an approval can never
            # outlive the content it approved.
            manifest = _store.get_manifest(plan_name)
            if reset_if_changed(manifest):
                _store.save_manifest(plan_name, manifest)
        if (
            was_dispatched
            and "agent_instructions" in fields
            and fields["agent_instructions"] != previous_instructions
        ):
            _notify_user(
                plan_name,
                f"{story_key} brief patched after dispatch "
                f"(agent_instructions changed via patch_story).",
                story_key=story_key,
                event="brief_patched",
                **(
                    {"correlation_id": story["correlation_id"]}
                    if story.get("correlation_id")
                    else {}
                ),
            )
        return {"ok": True, "story_key": story_key, "story": story}


# must follow the defs: service.py back-imports them, so defining them first
# keeps both import directions cycle-free
from .service import _ServerRef

_VALID_STORY_STATUSES = _ServerRef("_VALID_STORY_STATUSES")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
_PATCHABLE_STORY_FIELDS = _ServerRef("_PATCHABLE_STORY_FIELDS")
_VALID_STORY_BACKENDS = _ServerRef("_VALID_STORY_BACKENDS")
_notify_user = _ServerRef("_notify_user")
