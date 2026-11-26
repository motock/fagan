"""Failure-routing phases of check_story_status: an agent that died on an
LLM/Ollama transport error (infrastructure failure), or one that hit its step
cap. Both checkpoint the worktree for resume and may switch model or escalate.

Extracted verbatim from pipeline/story_status.py (behavior-preserving move).
Each phase returns a finished result dictionary, or None meaning "continue".
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import bound_records, server_bindings
from .config import (
    INFRA_FAILURE_FALLBACK_THRESHOLD,
    INFRA_FAILURE_LOG_SUBSTRING,
    STEP_CAP_FALLBACK_THRESHOLD,
    STEP_CAP_MARKERS,
)
from .git_ops import _commit_wip
from .rebrief import append_cleanup_guidance


def check_infra_failure(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
    worktree: Path,
    last_log_line: str,
) -> dict[str, Any] | None:
    # Infra-failure exit routing: a dispatch that died on an LLM/Ollama
    # transport error (after chat()'s own retries and the 5xx trim-retry are
    # exhausted) is not a review/test-quality outcome and must not be graded
    # or counted against rework_attempts - see INFRA_FAILURE_LOG_SUBSTRING's
    # docstring for the live incident this fixes.
    if INFRA_FAILURE_LOG_SUBSTRING not in last_log_line:
        return None
    _checkpoint_interrupted(
        plan_name,
        story_key,
        story,
        worktree,
        step="infra_failure",
        summary="Dispatch died on an infrastructure failure (LLM/Ollama "
        "transport error); checkpointed for resume.",
    )

    # See INFRA_FAILURE_FALLBACK_THRESHOLD: track consecutive infra
    # failures on the current model with SEPARATE streak fields from
    # STEP_CAP_MARKERS below - an infra death is not evidence the MODEL
    # is struggling, so it must never feed that branch's model-switch
    # logic (test_check_story_status_infra_failure_does_not_trigger_
    # model_fallback). Notify on the very first occurrence (unlike a
    # step-cap hit, which is routine for a local model, an infra death
    # this late - after chat()'s own retries AND the 5xx trim-retry are
    # exhausted - is unusual enough to be worth surfacing immediately),
    # then apply the same two remedies step-cap uses once the streak
    # crosses the threshold: switch to the plan's opted-in local fallback
    # model, or escalate to Claude.
    current_model = story.get("dispatched_model") or story.get("model")
    if story.get("infra_failure_streak_model") == current_model:
        story["infra_failure_streak"] = story.get("infra_failure_streak", 0) + 1
    else:
        story["infra_failure_streak"] = 1
        story["infra_failure_streak_model"] = current_model
    if story["infra_failure_streak"] == 1:
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} dispatch died on an infrastructure failure "
            f"(LLM/Ollama transport error) on {current_model}; resuming "
            f"from the last checkpoint.",
        )

    escalated = _infra_model_fallback(plan_name, story_key, story, manifest, pid, current_model)
    if escalated is not None:
        return escalated
    bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
    return {"status": "interrupted", "pid": pid, "reason": "infra_failure"}


def _infra_model_fallback(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
    current_model: str | None,
) -> dict[str, Any] | None:
    """Apply the local-fallback or Claude-escalation remedy once the infra streak
    reaches its threshold. Returns the escalated result (no manifest write, as
    before) when escalated, else None.
    """
    fallback_model = manifest.get("local_model_fallback")
    if (
        fallback_model
        and current_model != fallback_model
        and story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
        and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
    ):
        story["model"] = fallback_model
        story.pop("infra_failure_streak", None)
        story.pop("infra_failure_streak_model", None)
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
            f"infrastructure failures on {current_model}; switching to "
            f"fallback model {fallback_model} for the next resume.",
            story_key=story_key,
            event="model_fallback",
            **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
        )
        return None
    if (
        not fallback_model
        and server_bindings.server()._auto_escalation_enabled()
        and story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
        and not story.get("escalated")
        and server_bindings.server()._escalation_target() != (story.get("backend"), story.get("model"))
        and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
    ):
        server_bindings.server()._escalate_to_claude(manifest, plan_name, story_key)
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
            f"infrastructure failures on {current_model}; escalating to "
            f"{server_bindings.server()._escalation_label()} (no local_model_fallback configured).",
            story_key=story_key,
            event="escalated",
            **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
        )
        return {
            "status": "todo",
            "reason": "infra_failure_escalated_to_claude",
            "pid": pid,
        }
    return None


def check_step_cap(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
    worktree: Path,
    last_log_line: str,
) -> dict[str, Any] | None:
    # Step-cap exit routing (regression guard for PR #49 / commit 90a3cf1):
    # when the headless agent hits its step cap it prints a terminal marker
    # on the LAST line of agent.log, exits with code 2, and has already
    # WIP-committed. Classifying the run by its tail line (NOT a substring
    # search of the whole file — a resumed agent appends to agent.log, so an
    # old marker from a prior tick may appear earlier) lets us short-circuit
    # before the test suite runs. If we ran tests against the WIP commit and
    # it passed, we'd land the story on `tests_passed`, which is merge-
    # eligible — and that is exactly how incomplete step-capped work landed
    # on master. `interrupted` is dispatch-eligible, so the next
    # advance_pipeline tick resumes the agent in its existing worktree from
    # its WIP commit, seeded by the journal entry we write below.
    if last_log_line not in STEP_CAP_MARKERS:
        return None
    _checkpoint_interrupted(
        plan_name,
        story_key,
        story,
        worktree,
        step="step_cap_reached",
        summary="Agent hit the step cap; checkpointed for resume.",
    )

    # CLAUDE.md Step 9: diagnose where this implementer struggled and fold
    # the root cause into agent_instructions so the resume isn't a blind
    # retry. Runs before any model-switch/escalation below so the diagnosis
    # uses the model that just ran (the struggling one) and the worktree's
    # agent.log is still present for evidence. Fail-open: a None/errored
    # diagnosis leaves agent_instructions untouched (no-op).
    server_bindings.server()._rebrief_step_cap_struggle(
        story, str(worktree), plan_role_config=manifest.get("role_config"),
        plan_name=plan_name,
        story_key=story_key)
    # Worktree hygiene: append the cleanup-guidance section so the resumed
    # agent tidies the worktree before continuing. Unconditional and
    # idempotent (append_cleanup_guidance is a no-op when its header is
    # already present), so it composes safely with the diagnosis above.
    story["agent_instructions"] = append_cleanup_guidance(
        story.get("agent_instructions", ""))

    escalated = _step_cap_model_fallback(plan_name, story_key, story, manifest, pid)
    if escalated is not None:
        return escalated
    bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
    return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}


def _step_cap_model_fallback(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
) -> dict[str, Any] | None:
    """Switch to the plan's fallback model, or escalate to Claude, once the
    consecutive step-cap streak reaches STEP_CAP_FALLBACK_THRESHOLD. Returns the
    escalated result (no manifest write, as before) when escalated, else None.
    """
    # See STEP_CAP_FALLBACK_THRESHOLD: track consecutive step-cap
    # interrupts on the current model and, past the threshold, switch to
    # the plan's opted-in fallback model for the next resume. Worktree
    # and journal are left untouched so the resumed run still benefits
    # from whatever real progress is already committed.
    fallback_model = manifest.get("local_model_fallback")
    current_model = story.get("dispatched_model") or story.get("model")
    # STEP_CAP_MARKERS are only ever printed by the local agent scripts, so
    # a Claude-backend story should never reach here in practice - guard
    # explicitly anyway (defense in depth) so a plan-scoped local model
    # name can never land in a Claude story's model field. Missing
    # "backend" defaults to local: dispatch_story always sets it
    # explicitly, so an absent key only occurs in tests exercising this
    # branch in isolation.
    if (
        fallback_model
        and current_model != fallback_model
        and story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
    ):
        _bump_step_cap_streak(story, current_model)
        if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
            story["model"] = fallback_model
            story.pop("step_cap_streak", None)
            story.pop("step_cap_streak_model", None)
            server_bindings.server()._notify_user(
                plan_name,
                f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                f"on {current_model}; switching to fallback model "
                f"{fallback_model} for the next resume.",
                story_key=story_key,
                event="model_fallback",
                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
            )
        return None
    if (
        not fallback_model
        and server_bindings.server()._auto_escalation_enabled()
        and story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
        and not story.get("escalated")
        and server_bindings.server()._escalation_target() != (story.get("backend"), story.get("model"))
    ):
        # No local_model_fallback opt-in for this plan: under auto
        # dispatch, escalate to Claude instead of cycling on the same
        # struggling local model forever. Mutually exclusive with the
        # local-fallback branch above (gated on `not fallback_model`) -
        # no chaining from local fallback to Claude.
        _bump_step_cap_streak(story, current_model)
        if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
            server_bindings.server()._escalate_to_claude(manifest, plan_name, story_key)
            server_bindings.server()._notify_user(
                plan_name,
                f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                f"on {current_model}; escalating to {server_bindings.server()._escalation_label()} (no "
                f"local_model_fallback configured).",
                story_key=story_key,
                event="escalated",
                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
            )
            return {
                "status": "todo",
                "reason": "step_cap_escalated_to_claude",
                "pid": pid,
            }
    return None


def _bump_step_cap_streak(story: dict[str, Any], current_model: str | None) -> None:
    if story.get("step_cap_streak_model") == current_model:
        story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
    else:
        story["step_cap_streak"] = 1
        story["step_cap_streak_model"] = current_model


def _checkpoint_interrupted(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    worktree: Path,
    *,
    step: str,
    summary: str,
) -> None:
    """WIP-commit the worktree, journal the checkpoint, and mark the story
    interrupted with its commit and timestamp."""
    sha = _commit_wip(str(worktree), story_key, step)
    interrupted_at = datetime.now(UTC).isoformat()
    server_bindings.server()._store.append_journal(
        plan_name,
        story_key,
        {
            "step": step,
            "summary": summary,
            "next_hint": "",
            "commit": sha,
            "ts": interrupted_at,
        },
    )
    story["status"] = "interrupted"
    story["last_commit"] = sha
    story["interrupted_at"] = interrupted_at
