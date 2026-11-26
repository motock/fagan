"""Review-reuse guard of check_story_status: a story about to land on
tests_passed whose HEAD has not moved since the last REQUEST_CHANGES review is
a no-progress retry, not a new submission. It is counted against the rework
budget and either escalated, parked, or sent back for redispatch.

Extracted verbatim from pipeline/story_status.py (behavior-preserving move).
Returns a finished result dictionary, or None meaning "continue".
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from . import bound_records, server_bindings
from .no_progress import classify_no_progress
from .story_fields import merge_into_fresh_manifest


def check_no_new_commit_since_review(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree: Path,
    story_keys: tuple[str, ...],
) -> dict[str, Any] | None:
    # Mode 27 guard: the story is about to land on tests_passed (via either
    # path above — a genuine pass, or the PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL
    # opt-in surfacing a failing-but-reviewable submission) but HEAD is
    # unchanged since the last REQUEST_CHANGES recorded last_reviewed_sha —
    # the rework redispatch produced no new commit (e.g. it parked in a read
    # loop or crashed without committing, including an LLM transport error
    # mid-turn). Routing to tests_passed would hand review_story the same
    # SHA, where Mode 24's same-SHA skip guard loops forever (tests_passed is
    # not dispatch-eligible, so the story stalls invisibly — observed live
    # 2026-07-20 on the acceptance-fail-review path: 14+ consecutive silent
    # skip-notifications, the guard below originally covered only the
    # `passed=True` branch and missed this one). Route to changes_requested
    # so the scheduler redispatches, and count this no-progress retry against
    # the rework cap so a stuck agent parks rather than looping forever.
    if not (story["status"] == "tests_passed" and story.get("last_reviewed_sha")):
        return None
    head_res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if head_res.stdout.strip() != story["last_reviewed_sha"]:
        return None
    parked = classify_no_progress(
        plan_name, story_key, story, str(worktree), manifest)
    if parked is not None:
        return parked
    attempts = story.get("rework_attempts", 0) + 1
    story["rework_attempts"] = attempts
    # Zero commits since the last review is a stronger, distinct
    # signal from "made changes but the reviewer wasn't satisfied" -
    # see REWORK_MAX_ATTEMPTS_NO_COMMIT's docstring in config.py.
    # Applies uniformly regardless of escalation/oracle status.
    rework_cap = server_bindings.server().REWORK_MAX_ATTEMPTS_NO_COMMIT
    if attempts >= rework_cap:
        return _no_commit_budget_exhausted(plan_name, story_key, story, manifest, attempts)
    # LD90 W6: resuming the same transcript after a round that wrote
    # nothing replays the confusion that produced it. Drop the
    # transcript so the next rework starts cold from the brief at the
    # last commit, and fold a root-cause diagnosis into the brief
    # (same fail-open rebrief the step-cap path uses).
    (worktree / ".agent_transcript.json").unlink(missing_ok=True)
    server_bindings.server()._rebrief_step_cap_struggle(
        story, str(worktree), plan_role_config=manifest.get("role_config"),
        plan_name=plan_name, story_key=story_key)
    story["status"] = "changes_requested"
    # merge_into_fresh_manifest returns None when the fresh manifest is unreadable;
    # the put then receives it unchanged, as it did before this move.
    bound_records.put_manifest(server_bindings.server()._store, plan_name,
        merge_into_fresh_manifest(server_bindings.server()._store, plan_name, story_key, story,
            manifest, keys_at_entry=story_keys))  # type: ignore[arg-type]
    return {
        "status": "changes_requested",
        "reason": "no_new_commit_since_last_review",
    }


def _no_commit_budget_exhausted(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    # Mirror review_story's own rework-cap escalation (Mode 24/28):
    # a local agent that keeps parking/crashing without writing
    # code is exactly the same "local tier couldn't finish this"
    # signal as a reviewer's rework budget running out - give it
    # to Claude before parking for a human, under the same
    # PIPELINE_BACKEND_DISPATCH=auto opt-in. Root-caused live
    # 2026-07-24: this guard was the one park path in the file
    # missing the hook every other rework-exhaustion park path
    # already had (review_story's three call sites), so a story
    # that hit exactly this path never got a chance at Claude.
    if (
        server_bindings.server()._auto_escalation_enabled()
        and not story.get("escalated")
        and server_bindings.server()._escalation_target() != (story.get("backend"), story.get("model"))
    ):
        server_bindings.server()._escalate_review_to_claude(
            story,
            story_key,
            plan_name,
            f"no new commit after {attempts} rework redispatches",
        )
        story["status"] = "changes_requested"
        bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
        return {
            "status": "changes_requested",
            "reason": "no_new_commit_escalated_to_claude",
        }
    story["status"] = "parked"
    story["parked_reason"] = (
        f"no new commit after {attempts} rework redispatches - "
        "agent keeps parking/crashing without writing code."
    )
    bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
    server_bindings.server()._notify_user(
        plan_name,
        f"{story_key} parked: no new commit after {attempts} rework "
        f"redispatches - needs human review.",
        event="story_parked",
        story_key=story_key,
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    return {
        "status": "parked",
        "reason": "no_new_commit_rework_budget_exhausted",
    }


def route_acceptance_failure_to_review(
    story: dict[str, Any], story_key: str, worktree: Path, passed: bool
) -> None:
    # Opt-in review-on-acceptance-fail (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1):
    # route a dispatch whose acceptance oracle FAILED — but which produced real
    # work (new commits on the agent branch) — to review instead of straight to
    # "failed", so the reviewer evaluates the failing submission and the rework
    # loop re-dispatches the model up to REWORK_MAX_ATTEMPTS with the reviewer's
    # feedback. This engages the reviewer (previously unreachable for any
    # acceptance-failing cell: every such cell parked at "failed" with
    # rework_attempts=0, review_verdict=None, so the configured rework budget
    # and reviewer never ran — observed live, 2026-07-17, 0/9 mlx cells reached
    # review, zero GLM reviewer usage). Production-aligned: a reviewer sees
    # failing CI and REQUEST_CHANGES; the merge gate (_reverify_acceptance)
    # still blocks any APPROVEd-but-failing merge, so this never lands wrong
    # code. An empty-branch park (no real work) stays "failed" — re-dispatching
    # the same stuck prompt to the same model won't help. Opt-in so default
    # production behavior is unchanged; review_story's existing rework cap
    # (park/escalate after REWORK_MAX_ATTEMPTS) bounds the cycles.
    if (
        not passed
        and story["status"] == "failed"
        and os.environ.get("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "0") == "1"
        and server_bindings.server()._worktree_has_new_commits(
            worktree, story_key, base_branch=server_bindings.server()._default_branch()
        )
    ):
        story["status"] = "tests_passed"  # reviewable; reviewer sees the failure
        story["acceptance_failed_review"] = True

