"""Advance-owned merge-hold re-adjudication.

The names this module reads that are bound at module level in
``pipeline.advance`` (``PIPELINE_AUTONOMY``, ``_merge_decision``,
``_merge_mod``, ``merge_adjudication_plan``) are resolved through
``pipeline.advance`` at call time via ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.advance, NAME, ...)`` keeps landing.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef

MERGE_MAX_ATTEMPTS = _ModuleRef("pipeline.advance", "MERGE_MAX_ATTEMPTS")
_atomic_write_json = _ModuleRef("pipeline.advance", "_atomic_write_json")
_ci_pending_expired = _ModuleRef("pipeline.advance", "_ci_pending_expired")
_ci_rerun = _ModuleRef("pipeline.advance", "_ci_rerun")
_ci_rework_feedback = _ModuleRef("pipeline.advance", "_ci_rework_feedback")
_default_branch = _ModuleRef("pipeline.advance", "_default_branch")
_degraded_ci_branch = _ModuleRef("pipeline.advance", "_degraded_ci_branch")
_mark_plane_done = _ModuleRef("pipeline.advance", "_mark_plane_done")
_maybe_record_retro = _ModuleRef("pipeline.advance", "_maybe_record_retro")
_mcp_restart_notice = _ModuleRef("pipeline.advance", "_mcp_restart_notice")
_mcp_self_source_touched = _ModuleRef("pipeline.advance", "_mcp_self_source_touched")
_merge_gate_ci_status = _ModuleRef("pipeline.advance", "_merge_gate_ci_status")
_merge_pr = _ModuleRef("pipeline.advance", "_merge_pr")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_rebase_and_push_for_merge = _ModuleRef("pipeline.advance", "_rebase_and_push_for_merge")
_reverify_acceptance = _ModuleRef("pipeline.advance", "_reverify_acceptance")
_reverify_build = _ModuleRef("pipeline.advance", "_reverify_build")
_store = _ModuleRef("pipeline.advance", "_store")

PIPELINE_AUTONOMY = _ModuleRef("pipeline.advance", "PIPELINE_AUTONOMY")
_merge_decision = _ModuleRef("pipeline.advance", "_merge_decision")
_merge_mod = _ModuleRef("pipeline.advance", "_merge_mod")
merge_adjudication_plan = _ModuleRef("pipeline.advance", "merge_adjudication_plan")


_MERGE_HOLD_REASON = "high risk held for human review"


def _readjudicate_parked_merge_hold(
    plan_name: str, key: str, story: dict[str, Any]
) -> dict[str, Any] | None:
    """Re-run the merge gate for a parked high-risk hold whose evidence changed.

    MERGEPARK-2: a merge-gate park used to be terminal - the loop below
    skipped every story whose status was not ``pr_open``, so a story parked
    citing missing PR checks (WAP-1, parked 2026-09-15T01:26) was never
    revisited after ``gh pr checks`` turned green minutes later. A park is
    re-examined only when ALL of the following hold:

    * the park is the merge gate's own high-risk hold (the exact reason
      string - human parks, triage parks and any other reason are never
      touched);
    * the story is approved and carries a ``pr_url``;
    * the park recorded the evidence the ruling was made on
      (``merge_park_evidence`` - parks created before this feature, and
      human/triage parks, have none);
    * autonomy is ``full``: gated and dry-run never adjudicated in the first
      place, so they must never re-adjudicate - checked BEFORE any gather.

    The CURRENT single-poll CI state is gathered with the non-polling
    ``_ci_status_once`` (never the blocking ``_ci_status`` poller), the branch
    resolved exactly like the merge path below does. Only a DIFFERING state
    re-invokes the gate, so the cost is one overlord call per evidence
    TRANSITION, not per tick. A ``merge`` ruling clears the snapshot, flips
    the story to ``pr_open`` and lets the caller fall through into the same
    merge path a pr_open story takes; a ``park`` ruling leaves the story
    parked and the caller's park branch refreshes the snapshot. A gather
    failure leaves the old snapshot intact and the story parked - the tick
    must survive it.

    Returns the fresh gate decision, or ``None`` when there is nothing to
    re-adjudicate.
    """
    if story["status"] != "parked":
        return None
    if story.get("parked_reason") != _MERGE_HOLD_REASON:
        return None
    if story.get("review_verdict") != "APPROVE":
        return None
    if not story.get("pr_url"):
        return None
    snapshot = story.get("merge_park_evidence")
    if not isinstance(snapshot, dict):
        return None
    # Dereference once: PIPELINE_AUTONOMY is a _ServerRef proxy (see the
    # dry-run preview above for why the raw proxy is not used in comparisons
    # that outlive this expression).
    if PIPELINE_AUTONOMY._value() != "full":
        return None

    from .ci import _ci_status_once
    from .pr import _resolve_story_branch

    worktree = story.get("worktree", "")
    if worktree and Path(worktree).is_dir():
        branch = _resolve_story_branch(worktree, key)
    else:
        # No worktree to probe: hand the gather no branch at all, exactly
        # like the merge path - a locally computed convention branch is the
        # exact mistake the round-2 review finding names.
        branch = ""
    try:
        current = _ci_status_once(branch, sha="")
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning(
            "%s merge-park re-adjudication: CI gather failed; keeping the "
            "recorded evidence",
            key,
        )
        return None
    if current == snapshot.get("pr_checks"):
        # Flap guard: an unchanged state never re-invokes the overlord.
        return None

    logging.getLogger("pipeline").info(
        "%s merge-park evidence changed; re-adjudicating the hold", key
    )
    story["pr_checks"] = current
    with merge_adjudication_plan(plan_name), _merge_mod.merge_adjudication_story(key):
        decision = _merge_decision(story)
    if decision["action"] == "merge":
        story.pop("merge_park_evidence", None)
        story.pop("merge_parked_at", None)
        # Flip FIRST, so a merge path that then blocks (pending CI, failed
        # rebase) leaves the story in the ordinary pr_open state that path
        # already knows how to retry.
        story["status"] = "pr_open"
    else:
        # Stay parked: refresh the snapshot to the fresh value. This is the
        # flap guard - the next tick compares equal and never re-invokes the
        # gate, so the cost is one overlord call per evidence TRANSITION.
        story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}
        story["merge_parked_at"] = datetime.now(timezone.utc).isoformat()
    return decision


def _adjudicate_merges(plan_name: str, summary: dict[str, Any]) -> None:
    manifest_path = _store.manifest_path(plan_name)
    # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
    manifest = json.loads(manifest_path.read_text())
    stories = manifest["stories"]
    for key, story in stories.items():
        # MERGEPARK-2: a parked high-risk merge hold is re-examined when the
        # evidence the ruling cited changed. A fresh "merge" ruling flips the
        # story to pr_open and falls through into the SAME merge path below;
        # a fresh "park" ruling falls through to the park branch, which
        # refreshes the snapshot. ``decision`` is None when the story was not
        # re-adjudicated (or the gather failed), and the pr_open path then
        # runs the gate itself as before.
        decision = _readjudicate_parked_merge_hold(plan_name, key, story)
        if decision is None and story["status"] != "pr_open":
            continue
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
        if decision is None:
            with merge_adjudication_plan(plan_name), _merge_mod.merge_adjudication_story(
                key
            ):
                decision = _merge_decision(story)
        if decision["action"] != "merge":
            story["status"] = "parked"
            story["parked_reason"] = decision["reason"]
            # MERGEPARK-2: record the evidence this ruling was made on, so a
            # later tick can tell whether the picture actually changed (e.g.
            # checks pending at park time, green now) instead of the park
            # being terminal. Written in every mode: a gated park is
            # re-adjudicable too if autonomy is ever full again.
            story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}
            story["merge_parked_at"] = datetime.now(timezone.utc).isoformat()
            _notify_user(
                plan_name,
                f"{key} parked: {decision['reason']}",
                event="story_parked",
                story_key=key,
                **(
                    {"correlation_id": story["correlation_id"]}
                    if story.get("correlation_id")
                    else {}
                ),
            )
            summary["parked"].append(key)
            summary["notify"].append(key)
            continue

        # Mode 9: rebase onto current origin/master + CI gate before merge,
        # so a stale-base branch can't land cross-story breakage or a
        # ruff-red PR onto main. Failures count against merge_attempts just
        # like a transient `gh pr merge` failure (see MERGE_MAX_ATTEMPTS).
        worktree = story.get("worktree", "")
        # Resolve the worktree's ACTUAL HEAD branch (a rework round can
        # leave it on an alias agent/<key>-<suffix>) so the gate's rebase,
        # push, CI poll and _merge_pr all operate on the one branch
        # _merge_pr merges. The hardcoded convention name previously
        # named a branch a prior _merge_pr had already deleted ("src
        # refspec ... does not match any") or a stale twin, and the CI
        # poll queried a SHA that was never pushed to it. The resolver
        # itself fails open to the convention name when the worktree
        # cannot be probed, so no local fallback is needed here - and
        # none may be added: a locally computed convention branch is the
        # exact mistake the round-2 review finding names.
        from .pr import _resolve_story_branch

        if worktree and Path(worktree).is_dir():
            branch = _resolve_story_branch(worktree, key)
        else:
            # No worktree to probe (missing/anomalous): nothing was
            # dispatched, so no alias can exist. Hand the gate NO
            # branch at all - a locally computed convention branch is
            # the exact mistake the round-2 review finding names, and
            # the gate's own is_dir guard skips rebase/push for a
            # missing worktree without spawning a subprocess (the
            # CI-gate-disabled path must run zero subprocesses, see
            # test_advance_pipeline_ci_gate_disabled_skips_ci).
            branch = ""
        gate_error = ""
        ci_definitive_fail = False
        ci_wait = False
        # S5: a story already polling a pending CI run must not
        # re-rebase/force-push on every tick - with the non-blocking CI
        # poll that mints a new SHA whenever origin/master moved,
        # restarting CI and burning Actions minutes. Skip straight to
        # polling the exact SHA recorded on the first pending observation.
        if story.get("ci_pending_sha"):
            pushed_sha = story["ci_pending_sha"]
        else:
            gate_error, pushed_sha = _rebase_and_push_for_merge(plan_name, key, branch, worktree)
        if not gate_error:
            poll_branch = branch or _degraded_ci_branch(key)
            ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha)
            if ci["state"] == "cancelled" and not story.get(
                "ci_rerun_attempted"
            ):
                # Worth exactly one automatic rerun before treating it
                # as a failure - an abnormal queue delay can cancel
                # jobs with no code-quality signal at all.
                story["ci_rerun_attempted"] = True
                _ci_rerun(pushed_sha)
                ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha)
            if ci["state"] == "fail":
                gate_error = f"ci fail: {ci['error']}"
                # Only a genuine test-failure verdict is "definitive" -
                # cancelled (queue/infra flake, already given one
                # auto-rerun above) and pending are NOT, and must keep
                # retrying via the ordinary merge_attempts path below,
                # not consume rework budget.
                ci_definitive_fail = True
            elif ci["state"] == "cancelled":
                gate_error = f"ci fail: {ci['error']}"
            elif ci["state"] == "pending":
                story.setdefault("ci_pending_since", datetime.now(timezone.utc).isoformat())
                story["ci_pending_sha"] = pushed_sha
                if _ci_pending_expired(story["ci_pending_since"]):
                    _notify_user(
                        plan_name,
                        f"{key} CI has been pending since "
                        f"{story['ci_pending_since']} and exceeded the "
                        f"merge-gate pending bound; giving up on the wait.",
                        story_key=key,
                        severity="warning",
                        event="ci_pending_stalled",
                        dedup_key=f"ci_pending_stalled:{key}",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    story.pop("ci_pending_since", None)
                    story.pop("ci_pending_sha", None)
                    gate_error = f"ci pending: {ci['error']}"
                else:
                    ci_wait = True
            if ci["state"] != "pending":
                story.pop("ci_pending_since", None)
                story.pop("ci_pending_sha", None)
        if ci_wait:
            summary["ci_pending"].append(key)
            continue
        if not gate_error:
            # Independent of review: re-run the acceptance oracle
            # against the just-rebased branch right before merging.
            # Closes the gap CI alone can't (a repo without CI, or a
            # CI-independent slip between tests_passed and review).
            acc = _reverify_acceptance(story, worktree, key)
            if acc["state"] == "fail":
                gate_error = f"acceptance reverify fail: {acc['error']}"
        if not gate_error:
            # Independent of tests: a green suite doesn't mean the
            # project actually builds (PR #48 merged with `npm run
            # build` broken - retro §3.1).
            build = _reverify_build(worktree)
            if build["state"] == "fail":
                gate_error = f"build reverify fail: {build['error']}"

        if gate_error:
            # Opt-in (PIPELINE_REWORK_ON_CI_FAIL=1): a DEFINITIVE CI test
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
            rework_ok = (
                ci_definitive_fail
                and os.environ.get("PIPELINE_REWORK_ON_CI_FAIL", "0") == "1"
            )
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
                rework_ok = story.get("merge_attempts", 0) < MERGE_MAX_ATTEMPTS

            if rework_ok:
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
                story["review_feedback"] = _ci_rework_feedback(gate_error, attempts)
                story["status"] = "changes_requested"
                _notify_user(
                    plan_name,
                    f"{key} merge-gate CI failed ({gate_error}); "
                    f"routed to rework ({attempts}/{MERGE_MAX_ATTEMPTS}).",
                    event="merge_ci_rework",
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
                continue

            attempts = story.get("merge_attempts", 0) + 1
            story["merge_attempts"] = attempts
            if attempts >= MERGE_MAX_ATTEMPTS:
                story["status"] = "failed"
                story["merge_error"] = gate_error
                _notify_user(
                    plan_name,
                    f"{key} merge gate failed {attempts}x "
                    f"({gate_error}); giving up - needs human intervention.",
                    event="merge_gate_failed",
                    **(
                        {"correlation_id": story["correlation_id"]}
                        if story.get("correlation_id")
                        else {}
                    ),
                )
                summary["failed"].append(key)
            else:
                # leave pr_open; the next tick retries within budget.
                _notify_user(
                    plan_name,
                    f"{key} merge gate attempt {attempts}/"
                    f"{MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.",
                    event="merge_gate_retry",
                    **(
                        {"correlation_id": story["correlation_id"]}
                        if story.get("correlation_id")
                        else {}
                    ),
                )
            summary["notify"].append(key)
            continue

        # _merge_pr removes the worktree and deletes the branch, so the
        # self-source diff must be taken BEFORE the merge, not after.
        mcp_touched = _mcp_self_source_touched(
            worktree, f"origin/{_default_branch()}"
        )
        try:
            _merge_pr(story.get("worktree", ""), key)
        except Exception as e:  # noqa: BLE001 (gh/git transient failure - see MERGE_MAX_ATTEMPTS)
            attempts = story.get("merge_attempts", 0) + 1
            story["merge_attempts"] = attempts
            if attempts >= MERGE_MAX_ATTEMPTS:
                story["status"] = "failed"
                story["merge_error"] = str(e)
                _notify_user(
                    plan_name,
                    f"{key} merge failed {attempts}x "
                    f"({e}); giving up - needs human intervention.",
                    event="merge_failed",
                    **(
                        {"correlation_id": story["correlation_id"]}
                        if story.get("correlation_id")
                        else {}
                    ),
                )
                summary["failed"].append(key)
            else:
                # leave pr_open; the next tick retries within budget.
                _notify_user(
                    plan_name,
                    f"{key} merge attempt {attempts}/"
                    f"{MERGE_MAX_ATTEMPTS} failed ({e}); will retry.",
                    event="merge_retry",
                    **(
                        {"correlation_id": story["correlation_id"]}
                        if story.get("correlation_id")
                        else {}
                    ),
                )
            summary["notify"].append(key)
            continue
        story["status"] = "done"
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
            notify_if_plan_completed(plan_name, manifest)
        except Exception:
            logging.getLogger("pipeline").exception(
                "notify_if_plan_completed failed for %s", plan_name
            )
        if mcp_touched:
            _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
            summary["notify"].append(key)
        summary["merged"].append(key)
    _atomic_write_json(manifest_path, manifest)
