"""Readiness wait and merge phase of the advance merge adjudication loop.

Split out of ``pipeline.advance_merge._adjudicate_merges``. Names that tests
patch on ``pipeline.advance_merge`` are reached through that module at call
time (``_home``); the names bound in ``pipeline.advance`` go through
``_ModuleRef`` exactly as they do there.
"""

import logging
from dataclasses import dataclass
from typing import Any

from . import advance_merge as _home
from . import bound_records
from .ci import SettingsContext, gate_record
from .module_ref import _ModuleRef
from .telemetry import record_merge_result

_default_branch = _ModuleRef("pipeline.advance", "_default_branch")
_mark_plane_done = _ModuleRef("pipeline.advance", "_mark_plane_done")
_maybe_record_retro = _ModuleRef("pipeline.advance", "_maybe_record_retro")
_mcp_restart_notice = _ModuleRef("pipeline.advance", "_mcp_restart_notice")
_mcp_self_source_touched = _ModuleRef("pipeline.advance", "_mcp_self_source_touched")
_merge_pr = _ModuleRef("pipeline.advance", "_merge_pr")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_store = _ModuleRef("pipeline.advance", "_store")


@dataclass(frozen=True)
class Readiness:
    """Outcome of the readiness wait.

    The wait reloads the manifest, so the loop must continue with the
    reloaded ``manifest`` and ``story`` whether or not the story is
    ``finished`` for this tick (the old ``continue``).
    """

    finished: bool
    manifest: dict[str, Any]
    story: dict[str, Any]


def await_merge_readiness(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree: str,
    branch: str,
    settings: SettingsContext,
    summary: dict[str, Any],
) -> Readiness:
    """Wait (bounded, plan lock released) for the host to compute mergeability."""
    # Mergeability must be COMPUTED before `gh pr merge` will act. Right
    # after a push GitHub reports ``UNKNOWN`` while it computes, and gh's
    # merge command REFUSES instead of waiting - a refusal that used to be
    # counted as a failed merge attempt and, at MERGE_MAX_ATTEMPTS, fail a
    # PR that was in fact green and landable: PR #1618 (c1bfcfbf) burned
    # four attempts and PR #1621 (e0163dbc) three on 2026-11-08, both CLEAN
    # / MERGEABLE with all four required checks SUCCESS, both merged first
    # try by hand. So wait (bounded) for a computed answer: ``ready``
    # merges, ``blocked`` takes today's path below, and a budget that
    # expires while still ``pending`` spends NO attempt - the story stays
    # pr_open with a merge_pending_reason and the next tick retries.
    # ``pr_url`` gates the poll because a story with no recorded PR has
    # nothing to interrogate; it then behaves exactly as before.
    if not story.get("pr_url"):
        return Readiness(False, manifest, story)
    # Poll with the plan lock released so a human approve or mark-done
    # is not refused "plan busy". Persist this tick's edits first, then
    # apply the answer only if the story is still the polled story.
    polled = (story["status"], story.get("review_verdict_sha"))
    bound_records.put_manifest(_store, plan_name, manifest)
    with _home._released_plan_lock(plan_name):
        readiness = _home._await_merge_readiness(
            _home._merge_readiness_forge(), worktree, branch, settings
        )
    reloaded = bound_records.get_manifest(_store, plan_name)
    if reloaded is None:
        raise FileNotFoundError("manifest not found")
    # A human (or another tick) moved the story on: that wins, no-op.
    story = reloaded["stories"].get(key) or {}
    if (story.get("status"), story.get("review_verdict_sha")) != polled:
        return Readiness(True, reloaded, story)
    if readiness == "pending":
        story["merge_pending_reason"] = (
            "merge readiness still pending after "
            f"{_home._merge_readiness_timeout(settings):g}s"
        )
        # Deliberately NOT an attempt: this is a state the host has
        # not finished computing, not a refusal. The manifest write at
        # the end of this function persists the reason.
        summary.setdefault("merge_pending", []).append(key)
        return Readiness(True, reloaded, story)
    # Computed (or definitively blocked): a stale reason must not
    # outlive the wait.
    story.pop("merge_pending_reason", None)
    return Readiness(False, reloaded, story)


def _record_merge_failure(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    error: Exception,
    summary: dict[str, Any],
) -> None:
    attempts = story.get("merge_attempts", 0) + 1
    story["merge_attempts"] = attempts
    if attempts >= _home.MERGE_MAX_ATTEMPTS:
        story["status"] = "failed"
        story["merge_error"] = str(error)
        _notify_user(
            plan_name,
            f"{key} merge failed {attempts}x "
            f"({error}); giving up - needs human intervention.",
            event="merge_failed",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        summary["failed"].append(key)
        record_merge_result("failed")
    else:
        # leave pr_open; the next tick retries within budget.
        _notify_user(
            plan_name,
            f"{key} merge attempt {attempts}/"
            f"{_home.MERGE_MAX_ATTEMPTS} failed ({error}); will retry.",
            event="merge_retry",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
    summary["notify"].append(key)


def _record_merged(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    mcp_touched: Any,
    settings: SettingsContext,
    summary: dict[str, Any],
) -> None:
    story["status"] = "done"
    story["merge_gates"] = gate_record(settings)
    story.pop("merge_attempts", None)
    story.pop("parked_reason", None)
    story.pop("ci_rerun_attempted", None)
    story.pop("ci_rework", None)  # L1: clear the rework flag on done
    _mark_plane_done(key, plan_name)
    _notify_user(
        plan_name,
        f"{key} merged",
        story_key=key,
        event="story_merged",
        **(
            {"correlation_id": story["correlation_id"]}
            if story.get("correlation_id")
            else {}
        ),
    )
    # A fully-done self-repo plan must enter the retro backlog no
    # matter which path marked the last story done (dedup inside
    # _record_retro_pending makes repeat calls across ticks safe).
    _maybe_record_retro(plan_name, manifest)

    from .plan_completion import notify_if_plan_completed

    try:
        notify_if_plan_completed(plan_name, manifest, just_merged=key)
    except Exception:
        logging.getLogger("pipeline").exception(
            "notify_if_plan_completed failed for %s", plan_name
        )
    if mcp_touched:
        _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
        summary["notify"].append(key)
    summary["merged"].append(key)
    record_merge_result("merged")


def land_story(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree: str,
    settings: SettingsContext,
    summary: dict[str, Any],
) -> None:
    """Merge the PR and record the outcome (merged, retry, or failed)."""
    # _merge_pr removes the worktree and deletes the branch, so the
    # self-source diff must be taken BEFORE the merge, not after.
    mcp_touched = _mcp_self_source_touched(worktree, f"origin/{_default_branch()}")
    try:
        _merge_pr(story.get("worktree", ""), key)
    except Exception as e:  # noqa: BLE001 (gh/git transient failure - see MERGE_MAX_ATTEMPTS)
        _record_merge_failure(plan_name, key, story, e, summary)
        return
    _record_merged(plan_name, key, story, manifest, mcp_touched, settings, summary)
