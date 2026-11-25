"""PipelineService.set_story_status, get_story_diff and patch_story bodies, moved
out of pipeline/service.py (line-count target). The methods keep their signatures
and delegate here. Server-owned names are _ServerRef bindings, exactly as in
pipeline.service, so tests patching pipeline.server still land.
"""

import logging
from typing import Any

from pipeline import worktree_diff
from pipeline.actor import actor_id
from pipeline.concurrency import command_lock_wait_seconds
from pipeline.plan_approval import approval_enabled, reset_if_changed
from pipeline.triage_outcomes import record_resolution_if_ruled

logger = logging.getLogger(__name__)

#: The new statuses that mean the machine parked the story again.
_PARK_STATUSES = frozenset({"failed", "blocked", "parked"})
#: The new statuses that mean the story goes back to work.
_RETRY_STATUSES = frozenset({"todo", "interrupted", "changes_requested"})
#: The statuses a story is parked at, so leaving one answers a ruling.
_RULED_STATUSES = frozenset({"parked", "pr_open"})


def _human_action_for_status(status: str) -> str:
    """The human action that moving a story to ``status`` represents."""
    if status in _PARK_STATUSES:
        return "park"
    if status in _RETRY_STATUSES:
        return "retry"
    return "other"


def _story_status(plan_name: str, story_key: str) -> str | None:
    """The story's status as stored now, or ``None`` when it is not there.

    Callers validate the plan and story keys first: this reads the manifest by
    name. A missing plan reads as ``None`` rather than raising, so the caller's
    own refusal, not this read, answers an unknown plan. A store that cannot be
    read also reads as ``None``: the status only decides an audit record, and a
    dispatch must not fail because its record could not be written.
    """
    try:
        manifest = _store.get_manifest_or_none(plan_name) or {}
    except Exception:  # noqa: BLE001 - the status feeds an audit record only
        logger.warning(
            "could not read the story status for the human resolution",
            extra={"plan_name": plan_name, "story_key": story_key},
        )
        return None
    story = manifest.get("stories", {}).get(story_key)
    return story.get("status") if isinstance(story, dict) else None


def _prior_dispatch_status(plan_name: str, story_key: str) -> str | None:
    """The status a story has before a dispatch, validating the keys first.

    Validation runs here because the read is by name; a malformed key raises
    ``ValueError`` exactly as the dispatch itself does.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    return _story_status(plan_name, story_key)


def _record_dispatch_resolution(
    plan_name: str, story_key: str, prior_status: str | None, result: dict[str, Any]
) -> None:
    """Relaunching a parked story is the operator's answer to its ruling."""
    if prior_status == "parked" and result.get("ok") is True and not result.get("skipped"):
        record_resolution_if_ruled(
            plan_name,
            story_key,
            human_action="retry",
            new_status=_story_status(plan_name, story_key) or "",
            principal=actor_id(),
        )


def _record_interrupt_resolution(plan_name: str, story_key: str, was_parked: bool) -> None:
    """Stopping a parked story's agent is an answer the ruling did not foresee."""
    if was_parked:
        record_resolution_if_ruled(
            plan_name,
            story_key,
            human_action="other",
            new_status="interrupted",
            principal=actor_id(),
        )


def _set_story_status_impl(self, plan_name: str, story_key: str, status: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    if status not in _VALID_STORY_STATUSES:
        return {
            "ok": False,
            "error": f"invalid status {status!r}: "
            f"must be one of {sorted(_VALID_STORY_STATUSES)}",
        }

    wait_seconds = command_lock_wait_seconds()
    with _store.transaction(plan_name, wait_seconds) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "retry_after_seconds": wait_seconds,
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        previous_status = story.get("status")
        story["status"] = status
        if status != "parked":
            story.pop("parked_reason", None)
        _store.save_manifest(plan_name, manifest)

    if previous_status in _RULED_STATUSES and status != previous_status:
        record_resolution_if_ruled(
            plan_name,
            story_key,
            human_action=_human_action_for_status(status),
            new_status=status,
            principal=actor_id(),
        )
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

    wait_seconds = command_lock_wait_seconds()
    with _store.transaction(plan_name, wait_seconds) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "retry_after_seconds": wait_seconds,
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        before = _store.get_manifest(plan_name).get("stories", {}).get(story_key)
        previous_instructions = before.get("agent_instructions") if before else None
        was_dispatched = bool(before and before.get("dispatched_at"))
        was_parked = bool(before and before.get("status") == "parked")
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
                from pipeline.plan_approval_notice import notify_approval_event
                notify_approval_event(plan_name, manifest, "plan_pending_approval")
        brief_rewritten = (
            "agent_instructions" in fields
            and fields["agent_instructions"] != previous_instructions
        )
        if was_parked and brief_rewritten:
            # Rewriting a parked story's brief answers its ruling with a rebrief.
            record_resolution_if_ruled(
                plan_name,
                story_key,
                human_action="rebrief",
                new_status=story.get("status", "parked"),
                principal=actor_id(),
                brief_changed=True,
            )
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
