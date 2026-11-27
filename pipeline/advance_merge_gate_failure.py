"""Gate-failure phase of the advance merge adjudication loop.

Split out of ``pipeline.advance_merge._adjudicate_merges``. Names that tests
patch on ``pipeline.advance_merge`` are reached through that module at call
time (``_home``); the names bound in ``pipeline.advance`` go through
``_ModuleRef`` exactly as they do there.
"""

import os
from typing import Any

from . import advance_merge as _home
from .ci import SettingsContext
from .module_ref import _ModuleRef
from .rebase_conflict_feedback import _merge_ci_rework_feedback
from .telemetry import record_merge_result

_ci_rework_feedback = _ModuleRef("pipeline.advance", "_ci_rework_feedback")
_default_branch = _ModuleRef("pipeline.advance", "_default_branch")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")


def _route_conflict_rework(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    gate_error: str,
    summary: dict[str, Any],
) -> bool:
    """Send a merge-gate rebase conflict back to the implementer; True if routed."""
    # RC-02: a merge-gate REBASE CONFLICT can never be fixed by
    # retrying the identical merge - the branch still conflicts with
    # the default branch, so the story retries until merge_attempts
    # exhausts and parks (CL-12/CL-20). _try_auto_resolve_conflict only
    # handles pure additive-import conflicts, so a real conflict needs
    # the implementer told exactly what to do. Reuse the CI-rework
    # machinery (ci_rework raises the agent's done-bar to
    # full-suite-green; review_feedback carries the brief) but bound it
    # with its own conflict_reworks counter so a conflict rework does
    # NOT consume merge_attempts/rework_attempts: the agent commits new
    # code and the story must be able to come back through
    # tests -> review -> merge. Past the cap the ordinary
    # retry-then-park path below applies unchanged.
    conflict_max = _home._conflict_rework_max()
    conflict_ok = (
        gate_error.startswith("rebase:")
        and "conflict" in gate_error.lower()
        and os.environ.get("PIPELINE_REWORK_ON_CONFLICT", "1") != "0"
        and story.get("conflict_reworks", 0) < conflict_max
    )
    if not conflict_ok:
        return False
    conflict_attempt = story.get("conflict_reworks", 0) + 1
    story["conflict_reworks"] = conflict_attempt
    story["conflict_handoff"] = True
    story["ci_rework"] = True
    # Strip the gate's own "rebase: " label: git's output starts
    # with the first "CONFLICT (...)" line, and the brief's parser
    # anchors on that line start - leaving the prefix on would drop
    # the FIRST conflicted file from the brief and the notice.
    conflict_text = gate_error
    if conflict_text.startswith("rebase:"):
        conflict_text = conflict_text[len("rebase:"):].lstrip()
    story["review_feedback"] = _merge_ci_rework_feedback(
        story.get("review_feedback"),
        _home.conflict_rework_feedback(
            conflict_text,
            conflict_attempt,
            conflict_max,
            _default_branch(),
        ),
    )
    # A detached re-verify may still be running; it must not
    # outlive the story leaving pr_open.
    _rv_job = story.pop("merge_reverify", None)
    if _rv_job:
        _home.cancel_merge_reverify(_rv_job)
    story["status"] = "changes_requested"
    files = _home.parse_conflict_files(conflict_text)
    named = ", ".join(files) if files else "the conflicted files"
    _notify_user(
        plan_name,
        f"{key} merge-gate rebase conflict ({named}); "
        f"routed to rework ({conflict_attempt}/{conflict_max}).",
        event="merge_conflict_rework",
        story_key=key,
        **(
            {
                "correlation_id": story["correlation_id"],
                "attempt": story.get("dispatch_attempts", 0),
            }
            if story.get("correlation_id")
            else {}
        ),
    )
    summary["notify"].append(key)
    return True


def _route_ci_rework(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    gate_error: str,
    ci_definitive_fail: bool,
    settings: SettingsContext,
    summary: dict[str, Any],
) -> bool:
    """Send a definitive CI failure back for rework; True if routed."""
    # Opt-in (the rework_on_ci_fail setting, per repo): a DEFINITIVE CI test
    # failure - not a transient rebase/push error, not
    # pending/cancelled - can be caused by the agent's own
    # committed test file rather than the reviewed implementation
    # (the reviewer is acceptance-scoped and never saw it). Retrying
    # an unchanged branch identically MERGE_MAX_ATTEMPTS times can
    # never fix that; hand the CI failure back to the implementer as
    # rework feedback instead, bounded by the SAME rework budget
    # review_story uses, so a story that never converges still
    # parks/escalates rather than looping forever. See
    # MERGE_CI_REWORK_PLAN.md, 2026-07-17 (gpt-oss retry_backoff /
    # token_bucket: ground-truth-correct code abandoned because the
    # agent's own broken self-test tripped this gate).
    rework_ok = ci_definitive_fail and bool(settings.value("rework_on_ci_fail"))
    if rework_ok:
        # Bound by MERGE_MAX_ATTEMPTS via the merge_attempts
        # counter, which PERSISTS across the rework -> review
        # APPROVE -> merge-gate cycle. rework_attempts does NOT:
        # the review APPROVE path pops it on every pass (the
        # reviewer APPROVEs because it is acceptance-scoped and
        # the oracle is green), so reusing rework_attempts here
        # loops forever - each CI-fail re-increments 0->1 and the
        # cap never exhausts (verified 2026-07-17 on token_bucket:
        # four identical "routed to rework (1/3)" notifications,
        # same broken assertion every round). merge_attempts is
        # the merge gate's own counter and is not reset by review,
        # so it bounds the loop: MERGE_MAX_ATTEMPTS rework rounds,
        # then the fall-through below terminal-fails.
        rework_ok = story.get("merge_attempts", 0) < _home.MERGE_MAX_ATTEMPTS
    if not rework_ok:
        return False
    attempts = story.get("merge_attempts", 0) + 1
    story["merge_attempts"] = attempts
    # L1 (REVIEWER_ESCALATION_PLAN.md): flag this rework as
    # CI-triggered so the next dispatch_story raises the
    # agent's done-bar to full-suite-green (env
    # LOCAL_AGENT_REWORK_FULL_SUITE). Without it the rework
    # keeps the oracle-green bar and re-fails CI on the same
    # assertion every round (the agent's own broken test is
    # invisible to the acceptance-scoped oracle/reviewer).
    story["ci_rework"] = True
    story["review_feedback"] = _merge_ci_rework_feedback(
        story.get("review_feedback"),
        _ci_rework_feedback(gate_error, attempts),
    )
    # A detached re-verify may still be running; it must not
    # outlive the story leaving pr_open.
    _rv_job = story.pop("merge_reverify", None)
    if _rv_job:
        _home.cancel_merge_reverify(_rv_job)
    story["status"] = "changes_requested"
    _notify_user(
        plan_name,
        f"{key} merge-gate CI failed ({gate_error}); "
        f"routed to rework ({attempts}/{_home.MERGE_MAX_ATTEMPTS}).",
        event="merge_ci_rework",
        story_key=key,
        **(
            {
                "correlation_id": story["correlation_id"],
                "attempt": story.get("dispatch_attempts", 0),
            }
            if story.get("correlation_id")
            else {}
        ),
    )
    summary["notify"].append(key)
    return True


def _count_gate_attempt(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    gate_error: str,
    summary: dict[str, Any],
) -> None:
    """Spend one merge attempt on the gate failure; fail the story at the cap."""
    attempts = story.get("merge_attempts", 0) + 1
    story["merge_attempts"] = attempts
    if attempts >= _home.MERGE_MAX_ATTEMPTS:
        # A detached re-verify may still be running; it must not
        # outlive the story leaving pr_open.
        _rv_job = story.pop("merge_reverify", None)
        if _rv_job:
            _home.cancel_merge_reverify(_rv_job)
        story["status"] = "failed"
        story["merge_error"] = gate_error
        _notify_user(
            plan_name,
            f"{key} merge gate failed {attempts}x "
            f"({gate_error}); giving up - needs human intervention.",
            event="merge_gate_failed",
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
            f"{key} merge gate attempt {attempts}/"
            f"{_home.MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.",
            event="merge_gate_retry",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
    summary["notify"].append(key)


def handle_gate_failure(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    gate_error: str,
    ci_definitive_fail: bool,
    settings: SettingsContext,
    summary: dict[str, Any],
) -> None:
    """Route a gate failure: conflict rework, CI rework, or a counted attempt.

    Always finishes the story for this tick (the old ``continue``).
    """
    if _route_conflict_rework(plan_name, key, story, gate_error, summary):
        return
    if _route_ci_rework(
        plan_name, key, story, gate_error, ci_definitive_fail, settings, summary
    ):
        return
    _count_gate_attempt(plan_name, key, story, gate_error, summary)
