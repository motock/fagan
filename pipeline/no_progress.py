"""Classify a no-commit turn: an agent that reports a scope conflict is parked
for a human instead of being charged against the rework budget."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .build_detect import _last_done_summary
from .scope_conflict import parse_scope_conflict
from .service import _ServerRef

_atomic_write_json = _ServerRef("_atomic_write_json")
_notify_user = _ServerRef("_notify_user")

MAX_TRANSPORT_RETRIES = 5
_TRANSPORT_FAILURE_RE = re.compile(
    r"LLM call failed:.*?(?:\b429\b|\b5\d\d\b|timed out|ConnectError|ConnectionError)"
)
_TOOL_STEP_RE = re.compile(r"^\[step \d+\] [a-z_]+:", re.MULTILINE)


def is_transport_failure(log_tail: str) -> bool:
    """True when the current run died on an LLM transport failure before any tool ran."""
    segment = log_tail.rsplit("[boot]", 1)[-1]
    return bool(_TRANSPORT_FAILURE_RE.search(segment)) and not _TOOL_STEP_RE.search(segment)


def _read_log(worktree: str) -> str:
    try:
        return (Path(worktree) / "agent.log").read_text(errors="replace")
    except OSError:
        return ""


def _retry_transport(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
) -> dict[str, str]:
    """Redispatch without charging rework; park after MAX_TRANSPORT_RETRIES."""
    retries = int(story.get("transport_retries", 0)) + 1
    story["transport_retries"] = retries
    exhausted = retries >= MAX_TRANSPORT_RETRIES
    if exhausted:
        story["status"] = "parked"
        story["parked_reason"] = (
            f"transport failure: {MAX_TRANSPORT_RETRIES} consecutive dispatches never reached the model"
        )
    else:
        story["status"] = "changes_requested"
    _atomic_write_json(manifest_path, manifest)
    if exhausted:
        _notify_user(
            plan_name,
            f"{story_key} parked: {story['parked_reason']}.",
            event="story_parked",
            story_key=story_key,
        )
        return {"status": "parked", "reason": "transport_failure_exhausted"}
    _notify_user(
        plan_name,
        f"{story_key} dispatch hit an LLM transport failure before any step "
        f"(retry {retries}/{MAX_TRANSPORT_RETRIES}); redispatching.",
        event="transport_retry",
        story_key=story_key,
    )
    return {"status": "changes_requested", "reason": "transport_failure"}


def classify_no_progress(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    worktree: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> dict[str, str] | None:
    """Park the story if the last DONE summary carries SCOPE-CONFLICT markers.

    Returns the check_story_status result dict when parked, else None (the
    caller then applies the normal no-new-commit handling).
    """
    try:
        summary = _last_done_summary(Path(worktree) / "agent.log")
    except OSError as exc:
        logging.getLogger("pipeline").warning(
            "could not read agent log for scope-conflict check",
            extra={"plan": plan_name, "story_key": story_key, "error_type": type(exc).__name__},
        )
        return None
    conflicts = parse_scope_conflict(summary)
    if not conflicts:
        if is_transport_failure(_read_log(worktree)):
            return _retry_transport(plan_name, story_key, story, manifest, manifest_path)
        return None
    paths = [c.path for c in conflicts]
    story["scope_conflict"] = {
        "paths": paths,
        "reasons": {c.path: c.reason for c in conflicts},
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    story["status"] = "parked"
    story["parked_reason"] = f"scope conflict: {', '.join(paths)}"
    _atomic_write_json(manifest_path, manifest)
    _notify_user(
        plan_name,
        f"{story_key} parked: agent reported a scope conflict ({', '.join(paths)}) "
        "- needs a plan change.",
        event="story_parked",
        story_key=story_key,
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    return {"status": "parked", "reason": "scope_conflict"}
