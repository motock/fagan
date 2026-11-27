"""Hold and binding phases of the advance merge adjudication loop.

Split out of ``pipeline.advance_merge._adjudicate_merges``. Names that tests
patch on ``pipeline.advance_merge`` are reached through that module at call
time (``_home``); the names bound in ``pipeline.advance`` go through
``_ModuleRef`` exactly as they do there.
"""

from datetime import UTC, datetime
from typing import Any

from . import access_hooks, external_merge, merge_local_checks
from . import advance_merge as _home
from .module_ref import _ModuleRef
from .telemetry import record_merge_result

_degraded_ci_branch = _ModuleRef("pipeline.advance", "_degraded_ci_branch")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_merge_decision = _ModuleRef("pipeline.advance", "_merge_decision")
_merge_mod = _ModuleRef("pipeline.advance", "_merge_mod")
merge_adjudication_plan = _ModuleRef("pipeline.advance", "merge_adjudication_plan")


def _park_for_decision(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    decision: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    # A detached re-verify may still be running; it must not outlive
    # the story leaving pr_open.
    _rv_job = story.pop("merge_reverify", None)
    if _rv_job:
        _home.cancel_merge_reverify(_rv_job)
    _lc_job = story.pop("merge_local_checks_job", None)
    if _lc_job:
        _home.cancel_merge_reverify(_lc_job)
    story["status"] = "parked"
    story["parked_reason"] = decision["reason"]
    # MERGEPARK-2: record the evidence this ruling was made on, so a
    # later tick can tell whether the picture actually changed (e.g.
    # checks pending at park time, green now) instead of the park
    # being terminal. Written in every mode: a gated park is
    # re-adjudicable too if autonomy is ever full again.
    story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}
    story["merge_parked_at"] = datetime.now(UTC).isoformat()
    _notify_user(
        plan_name,
        f"{key} parked: {decision['reason']}"
        + (f" — {story['pr_url']}" if story.get("pr_url") else ""),
        event="story_parked",
        story_key=key,
        **(
            {"correlation_id": story["correlation_id"]}
            if story.get("correlation_id")
            else {}
        ),
    )
    summary["parked"].append(key)
    record_merge_result("parked")
    summary["notify"].append(key)


def evaluate_merge_hold(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> bool:
    """Decide whether this story may proceed to the merge gates this tick.

    Returns True when the story is finished for this tick (the old
    ``continue``): completed outside, not pr_open, waiting on local checks, or
    parked by the ruling. Returns False when the ruling is ``merge``.
    """
    # MERGEPARK-2: a parked high-risk merge hold is re-examined when the
    # evidence the ruling cited changed. A fresh "merge" ruling flips the
    # story to pr_open and falls through into the SAME merge path below;
    # a fresh "park" ruling falls through to the park branch, which
    # refreshes the snapshot. ``decision`` is None when the story was not
    # re-adjudicated (or the gather failed), and the pr_open path then
    # runs the gate itself as before.
    # A parked story whose PR a person merged on the forge is completed here.
    if external_merge.finish_if_merged_outside(plan_name, key, story, manifest, summary):
        return True
    decision = _home._readjudicate_parked_merge_hold(plan_name, key, story)
    if decision is None and story["status"] != "pr_open":
        return True
    # A fresh ruling is already in hand: a "merge" ruling left the story
    # pr_open and falls into the SAME merge path below; a "park" ruling
    # falls into the park branch below, which records the park and
    # refreshes the snapshot. Either way the gate is not asked twice.
    # Thread the real plan name into the merge gate without changing the
    # call arity: several long-standing tests (and the dry-run preview
    # below) call/patch ``_merge_decision`` with a one-argument callable,
    # so a second positional argument would break them. The explicit
    # ``plan_name`` parameter stays for direct callers; production flows
    # the name through this context, which ``_adjudicate_merges`` owns.
    if (
        decision is None
        and story["status"] == "pr_open"
        and merge_local_checks.adjudication_due(story, plan_name)
    ):
        # The repo's own tests, build and lint run on the branch head before
        # the overlord rules on a high-risk merge. A wait is no verdict yet:
        # nothing is ruled, parked or merged, and the manifest write at the
        # end of this function still persists the job handle (ci_wait).
        local = merge_local_checks.local_checks_gate(plan_name, key, story, summary)
        if local is not None and local["action"] == "wait":
            return True
        decision = local
    if decision is None:
        with merge_adjudication_plan(plan_name), _merge_mod.merge_adjudication_story(
            key
        ):
            decision = access_hooks.merge_decision(_merge_decision, plan_name, story)
    if decision["action"] != "merge":
        _park_for_decision(plan_name, key, story, decision, summary)
        return True
    return False


def check_review_binding(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    branch: str,
    summary: dict[str, Any],
) -> bool:
    """Verify the approval covers the branch head about to be merged.

    Returns True when the story is finished for this tick (re-review
    requested, or the binding was denied and parked).
    """
    from .review_merge_binding import (
        REVIEW_REBIND_MAX_ATTEMPTS,
        binding_denial_action,
        branch_head_sha,
        check_with_worktree,
        deny,
    )

    # The approval must cover the commit being merged: probe the remote
    # tip AFTER fetch and BEFORE any rebase/checkpoint/push, and refuse
    # here so nothing is rebased or pushed first. Keying this gate on
    # `branch` alone conflated "no local worktree" (a missing checkout)
    # with "no branch to bind against": the branch still exists remotely,
    # so the gate probes the same fallback name the CI poll below uses
    # instead of merging with zero binding verification.
    gate_branch = branch or _degraded_ci_branch(key)
    head = branch_head_sha(gate_branch)
    binding = check_with_worktree(story, head, story.get("worktree", ""))
    if binding["ok"]:
        return False
    # b4 evidence (2026-11-09/10): the recovery from a stale approval
    # was a MANUAL forced re-review, which APPROVED the new head -
    # `deny`'s premise that a retry cannot re-earn the review is
    # false, `review_story` exists. So an APPROVE whose head simply
    # moved is sent back through the normal review path (up to
    # REVIEW_REBIND_MAX_ATTEMPTS times); anything else parks as today.
    if (
        binding_denial_action(story, story.get("review_rebind_attempts", 0))
        == "rereview"
        # The b4 shape is a head that moved AFTER an approval: the
        # evidence is a recorded `last_reviewed_sha` (written when a
        # review cycle graded a head). With no such record the approval
        # never covered this branch head at all - not something a
        # re-review of the SAME unchanged state can remedy - so the
        # gate parks exactly as it did before this change.
        and story.get("last_reviewed_sha")
    ):
        # Back to the review path. BOTH shas must go: a surviving
        # review_verdict_sha would let the OLD approval cover the NEW
        # head, and a surviving last_reviewed_sha would make the
        # re-review skip the head as "unchanged since the last review".
        story["status"] = "tests_passed"
        story.pop("review_verdict_sha", None)
        story.pop("last_reviewed_sha", None)
        story["review_rebind_attempts"] = story.get("review_rebind_attempts", 0) + 1
        _notify_user(
            plan_name,
            f"{key} review_rebind: the branch head moved after the "
            f"approval ({(head or '')[:8]}); re-reviewing it "
            f"automatically, attempt "
            f"{story['review_rebind_attempts']}/{REVIEW_REBIND_MAX_ATTEMPTS}.",
            event="review_rebind",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        return True
    deny(plan_name, key, story, head, binding, summary)
    return True
