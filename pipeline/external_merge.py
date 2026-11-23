"""Completes a parked story whose pull request a person merged on the forge.

A team whose access grant lets the pipeline open pull requests but not merge
them parks the story (``access refused: <token>``) with its PR open and its
review approved. When a person then merges that PR on the forge, nothing in the
merge path runs again, so the story stayed parked for ever.

``finish_if_merged_outside`` is called by the advance loop for each story. It
asks the forge about a parked story that has a ``pr_url``, at most once per
story per ``POLL_INTERVAL_S`` seconds, and completes the story only when the
forge reports the PR as MERGED. Every other answer, and every forge failure,
leaves the story untouched. It never raises into the advance loop.
"""

import logging
import time
from typing import Any

from .ci import gate_record, settings_for_plan
from .forge import get_forge
from .persistence import _notify_user
from .pr import (
    _convention_branch,
    _remove_story_checkout,
    _repo_root,
    _resolve_story_branch,
)
from .telemetry import record_merge_result

logger = logging.getLogger("pipeline")

# The forge is asked about one story at most this often (seconds).
POLL_INTERVAL_S = 60.0

# Monotonic clock; tests replace it to move time without sleeping.
_clock = time.monotonic

# (plan, story key) -> monotonic time of the last forge question for it.
_last_checked: dict[tuple[str, str], float] = {}

# Fields a park leaves behind that no longer describe the story once it is done.
_PARK_FIELDS = (
    "merge_attempts",
    "parked_reason",
    "merge_parked_at",
    "merge_park_evidence",
    "ci_rerun_attempted",
    "ci_rework",
)


def finish_if_merged_outside(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> bool:
    """Finish ``story`` when its parked PR was merged on the forge.

    Returns True only when the story was completed here. Stories that are not
    parked, have no ``pr_url``, or were asked about less than
    ``POLL_INTERVAL_S`` ago are not sent to the forge at all.
    """
    if story.get("status") != "parked":
        return False
    pr_url = story.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        return False

    now = _clock()
    slot = (plan_name, key)
    last = _last_checked.get(slot)
    if last is not None and now - last < POLL_INTERVAL_S:
        return False
    _last_checked[slot] = now

    try:
        state = get_forge(_repo_root()).pr_state(pr_url)
    except Exception as exc:  # noqa: BLE001 - the advance loop must not see this
        # Only the exception type is logged: its message can carry a credential.
        logger.warning(
            "external merge check failed for %s in plan %s: %s",
            key, plan_name, type(exc).__name__,
        )
        return False
    if state == "MERGED":
        _finish_outside_merge(plan_name, key, story, manifest, summary)
        return True
    if state == "UNKNOWN":
        logger.warning("forge could not report the PR state for %s in plan %s", key, plan_name)
    return False


def _finish_outside_merge(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """Finish the story the way a successful pipeline merge does."""
    from .merge import _maybe_record_retro
    from .plan_completion import notify_if_plan_completed
    from .ticketing import _mark_plane_done

    worktree = story.get("worktree") or ""
    if worktree:
        branch = _resolve_story_branch(worktree, key)
    else:
        branch = _convention_branch(key)
    try:
        _remove_story_checkout(worktree, branch)
    except OSError as exc:
        logger.warning(
            "worktree cleanup failed for %s in plan %s: %s",
            key, plan_name, type(exc).__name__,
        )

    settings = settings_for_plan(plan_name)
    story["status"] = "done"
    story["merge_gates"] = gate_record(settings)
    for field in _PARK_FIELDS:
        story.pop(field, None)
    _mark_plane_done(key, plan_name)
    _notify_user(
        plan_name,
        f"{key} pull request was merged outside the pipeline; story completed.",
        story_key=key,
        event="story_merged_outside_pipeline",
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    _maybe_record_retro(plan_name, manifest)
    try:
        notify_if_plan_completed(plan_name, manifest, just_merged=key)
    except Exception:
        logger.exception("notify_if_plan_completed failed for %s", plan_name)
    summary["merged"].append(key)
    record_merge_result("merged")
