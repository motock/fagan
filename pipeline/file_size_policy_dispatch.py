"""Resolve and record ``file_size_policy`` for one dispatch.

Glue between the pure resolver (``pipeline.file_size_policy``) and dispatch:
stamps the result on the story and notifies the user when a policy refused a
requested loosening. The settings are read by the caller and passed in.
"""

import os
from datetime import UTC, datetime

from .file_size_policy import (
    FILE_SIZE_POLICY_ADVISORY,
    FILE_SIZE_POLICY_ENFORCED,
    resolve_file_size_policy,
)
from .service import _ServerRef

_notify_user = _ServerRef("_notify_user")
FILE_SIZE_POLICY_ENV = "PIPELINE_FILE_SIZE_POLICY"

_IMPLEMENTER_NOTE = (
    "File size: this deployment's file_size_policy is advisory. Prefer a new "
    "module over growing a file past about 1000 lines, but never split unrelated "
    "code, never restructure a file just to meet the line count, and never stop "
    "or park a story because of file size.\n\n"
)
_REVIEWER_NOTE = (
    "File size: this deployment's file_size_policy is advisory. A file over "
    "about 1000 lines is at most a Suggestion or a Nit. Never report file size as "
    "Blocking and never REQUEST_CHANGES on file size alone.\n"
)
_ADVISORY_NOTES = {"implementer": _IMPLEMENTER_NOTE, "reviewer": _REVIEWER_NOTE}


def _lock_rejected_message(story_key, rejections, policy):
    refused = ", ".join(f"{r['layer']}->{r['by']}" for r in rejections)
    return (
        f"{story_key} file_size_policy request refused by policy: {refused}, "
        f"running {policy}"
    )


def stamp_file_size_policy(plan_name, story_key, settings, story):
    result = resolve_file_size_policy(
        settings=settings,
        env_value=os.environ.get(FILE_SIZE_POLICY_ENV),
    )
    result["resolved_at"] = datetime.now(UTC).isoformat()
    story["file_size_policy_resolution"] = result
    if result["lock_rejections"]:
        _notify_user(
            plan_name,
            _lock_rejected_message(
                story_key, result["lock_rejections"], result["file_size_policy"]
            ),
            story_key=story_key,
            severity="info",
            event="file_size_policy_lock_rejected",
            dedup_key=f"file_size_policy_lock_rejected:{plan_name}:{story_key}",
        )
    return result


def file_size_policy_of(story):
    resolution = story.get("file_size_policy_resolution")
    if isinstance(resolution, dict) and (
        resolution.get("file_size_policy") == FILE_SIZE_POLICY_ENFORCED
    ):
        return FILE_SIZE_POLICY_ENFORCED
    return FILE_SIZE_POLICY_ADVISORY


def file_size_advisory_note(story, audience):
    resolution = story.get("file_size_policy_resolution")
    if not isinstance(resolution, dict):
        return ""
    if resolution.get("file_size_policy") != FILE_SIZE_POLICY_ADVISORY:
        return ""
    return _ADVISORY_NOTES.get(audience, "")
