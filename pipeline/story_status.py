"""
check_story_status: poll a dispatched agent's story, run its tests in the
worktree, and report pass/fail without auto-merging.

Extracted verbatim from pipeline/server.py (behavior-preserving file move).
"""

import json  # noqa: F401
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.dispatch import _find_dead_new_functions

from .build_detect import (
    _acceptance_rel_paths,
    _added_pytest_test_paths,
    _is_pytest_cmd,
    _scope_test_cmd_to_acceptance,
    failed_node_ids,  # noqa: F401
)
from .checkpoint import _terminate_and_checkpoint
from .ci import _acceptance_tampered
from .concurrency import _heavy_lock
from .config import (
    _LOCAL_BACKEND_NAMES,
    DISPATCH_MAX_ATTEMPTS,
    DISPATCH_STALE_ACTIVITY_SECONDS,
    DISPATCH_STARTUP_GRACE_SECONDS,
    DISPATCH_WATCHDOG_SECONDS,
    INFRA_FAILURE_FALLBACK_THRESHOLD,
    INFRA_FAILURE_LOG_SUBSTRING,
    REWORK_MAX_ATTEMPTS_NO_COMMIT,
    STEP_CAP_FALLBACK_THRESHOLD,
    STEP_CAP_MARKERS,
)
from .detached_grade import (
    GRADE_WRAPPER,  # noqa: F401
    _baseline_exempted_failures,
    collect_detached_grade,
    start_detached_grade,
)
from .detached_grade import (
    _detached_grade_lifecycle as _detached_grade_lifecycle_unbound,
)
from .escalation import (
    _escalate_review_to_claude,
    _escalate_to_claude,
    _escalation_label,
    _escalation_target,
)
from .git_ops import _commit_wip, _untrack_scratchpad
from .parsers import (
    _atomic_write_json,
    _is_give_up_summary,
    _validate_key,
)
from .plan_conflict_ruling import _plan_conflict_intercept
from .rebrief import append_cleanup_guidance
from .wedge_io import collect_story_wedge_signals

# The detached-grading watchdog reuses the dispatch watchdog's threshold so a
# single policy governs both "how long may a grade/dispatch stay outstanding"
# decisions (no new env var — see DETACHED_GRADE_WATCHDOG_SECONDS's use in
# check_story_status's dead-pid recovery path).
DETACHED_GRADE_WATCHDOG_SECONDS = DISPATCH_WATCHDOG_SECONDS


def _record_test_check(
    story: dict, test_cmd: list[str], test_dir, test_result, worktree: str
) -> str | None:
    """Persist this run's test result on the story, regardless of pass/fail.

    Returns the worktree's HEAD sha (or None when it cannot be read) so the
    caller can stamp the lint/dead-code caches with the same revision.
    """
    check_sha = None
    if worktree and os.path.isdir(worktree):
        try:
            check_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            check_sha = None
    story["last_test_check"] = {
        "cmd": test_cmd,
        "cwd": str(test_dir),
        "returncode": test_result.returncode,
        "stdout_tail": (test_result.stdout or "")[-2000:],
        "stderr_tail": (getattr(test_result, "stderr", "") or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
        "sha": check_sha,
    }
    return check_sha


def _clear_failure_streaks(story: dict) -> None:
    """Clear the consecutive-failure streaks a completed grade breaks."""
    for _streak_key in (
        "dispatch_attempts",
        "watchdog_streak",
        "step_cap_streak",
        "step_cap_streak_model",
        "infra_failure_streak",
        "infra_failure_streak_model",
    ):
        story.pop(_streak_key, None)


def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = _store.manifest_path(plan_name)  # noqa: F821
    manifest = _store.get_manifest(plan_name)  # noqa: F821
    story = manifest["stories"].get(story_key)
    if not story or "pid" not in story:
        return {"ok": False, "error": "Story not dispatched"}
    if story["status"] == "interrupted":
        # Incomplete by definition — running tests here would just record a
        # spurious failure instead of leaving it resumable.
        return {"status": "interrupted", "pid": story["pid"]}

    pid = story["pid"]
    try:
        os.kill(pid, 0)
        # os.kill succeeds for zombie (defunct) processes too — check ps stat
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
        stat = ps.stdout.strip()
        if stat and not stat.startswith("Z"):
            dispatched_at = story.get("dispatched_at")
            if dispatched_at is not None:
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(dispatched_at)
                ).total_seconds()
                # Determine if activity is stale
                signals = collect_story_wedge_signals(plan_name, story_key, story)
                activity_age = signals.get("activity_age_seconds")
                # A redispatch reuses the worktree, so agent.log can carry an
                # mtime from a PREVIOUS attempt far older than this dispatch.
                # Activity cannot be staler than the dispatch that produced
                # it: clamp to `elapsed` so a freshly launched agent is never
                # killed before it has had a chance to write. Fires only
                # once THIS dispatch has itself been quiet past the
                # threshold.
                if activity_age is not None:
                    activity_age = min(activity_age, elapsed)
                watchdog_summary = None
                dispatch_error = None
                if activity_age is not None and activity_age > DISPATCH_STALE_ACTIVITY_SECONDS:
                    watchdog_summary = (
                        f"Dispatch watchdog: no activity for {activity_age:.0f}s "
                        f"(stale-activity watchdog); elapsed {elapsed:.0f}s; "
                        f"process terminated."
                    )
                    dispatch_error = (
                        f"watchdog killed after {activity_age:.0f}s with no activity"
                    )
                # Fallback to wall-clock watchdog if activity is unknown or not stale
                elif activity_age is None and elapsed > DISPATCH_WATCHDOG_SECONDS:
                    watchdog_summary = (
                        f"Dispatch watchdog: no completion after {elapsed:.0f}s; process terminated."
                    )
                    dispatch_error = (
                        f"watchdog killed after {elapsed:.0f}s with no completion"
                    )
                if watchdog_summary is not None:
                    # OA2-03: every termination path must increment a counter
                    # that participates in a streak/cap, or a story that keeps
                    # hanging resumes the same struggling model forever.
                    # Increment BEFORE the checkpoint so the persisted state
                    # carries the new value - incrementing after would lag by
                    # one kill and the threshold escalation would never fire.
                    story["watchdog_streak"] = story.get("watchdog_streak", 0) + 1
                    _terminate_and_checkpoint(
                        manifest,
                        manifest_path,
                        plan_name,
                        story_key,
                        story,
                        pid=pid,
                        step="dispatch_watchdog_timeout",
                        summary=watchdog_summary,
                    )
                    _notify_user(  # noqa: F821
                        plan_name,
                        f"{story_key} killed by the dispatch watchdog; the "
                        f"re-dispatch counts as a rework cycle.",
                        story_key=story_key,
                        severity="warning",
                        event="dispatch_watchdog_timeout",
                        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
                    )
                    _rebrief_step_cap_struggle(  # noqa: F821
                        story, str(Path(story["worktree"])),
                        plan_role_config=manifest.get("role_config"),
                        plan_name=plan_name,
                        story_key=story_key)
                    story["dispatch_error"] = dispatch_error
                    # OA2-03 convergence: at the threshold, stop resuming the
                    # same struggling local model. Produce the SAME
                    # fallback/escalation signal the step-cap streak path
                    # produces at STEP_CAP_FALLBACK_THRESHOLD - a plain resume
                    # here would let a story that keeps hanging loop forever.
                    # The counter is deliberately NOT reset on this path: a
                    # later kill must keep escalating, not restart the count.
                    fallback_model = manifest.get("local_model_fallback")
                    current_model = story.get("dispatched_model") or story.get("model")
                    if story["watchdog_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                        if (
                            fallback_model
                            and current_model != fallback_model
                            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
                        ):
                            story["model"] = fallback_model
                            _notify_user(  # noqa: F821
                                plan_name,
                                f"{story_key} hit the watchdog kill {STEP_CAP_FALLBACK_THRESHOLD}x "
                                f"on {current_model}; switching to fallback model "
                                f"{fallback_model} for the next resume.",
                                story_key=story_key,
                                event="model_fallback",
                                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
                            )
                        elif (
                            not fallback_model
                            and _auto_escalation_enabled()  # noqa: F821
                            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
                            and not story.get("escalated")
                            and _escalation_target() != (story.get("backend"), story.get("model"))
                        ):
                            _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
                            _notify_user(  # noqa: F821
                                plan_name,
                                f"{story_key} hit the watchdog kill {STEP_CAP_FALLBACK_THRESHOLD}x "
                                f"on {current_model}; escalating to {_escalation_label()} (no "
                                f"local_model_fallback configured).",
                                story_key=story_key,
                                event="escalated",
                                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
                            )
                            _atomic_write_json(manifest_path, manifest)
                            return {
                                "status": "todo",
                                "reason": "step_cap_escalated_to_claude",
                                "pid": pid,
                            }
                    _atomic_write_json(manifest_path, manifest)
                    return {
                        "status": "interrupted",
                        "pid": pid,
                        "watchdog_killed": True,
                    }
                # No watchdog trigger
                return {"status": "running", "pid": pid}
            return {"status": "running", "pid": pid}
        # process is zombie or gone — fall through to test detection
    except ProcessLookupError:
        pass

    worktree = Path(story["worktree"])
    agent_log = worktree / "agent.log"
    if agent_log.exists() and agent_log.stat().st_size == 0:
        # Empty log within the startup grace window means the agent is alive
        # and bootstrapping - its first print() hasn't flushed yet, especially
        # when queued on Ollama's -np 1 worker behind another request. The
        # PID-alive check above already passed, so trust that and don't burn
        # dispatch_attempts on a process that's just slow to print. After the
        # grace window elapses with the log still empty, the agent is
        # presumed genuinely dead (failed launch) and we count it.
        log_age = time.time() - agent_log.stat().st_mtime
        if log_age < DISPATCH_STARTUP_GRACE_SECONDS:
            return {"status": "running", "pid": pid}
        # The agent process exited without ever writing a byte of output -
        # a failed launch, not a real attempt. Running tests against the
        # untouched worktree would just record a misleading "failed" for
        # work that was never tried. Within the dispatch error budget we keep
        # it "interrupted" (dispatch-eligible like "todo", so the next tick
        # retries it); once the budget is spent, a launch that never works
        # becomes a terminal "failed" so it stops looping forever.
        attempts = story.get("dispatch_attempts", 0) + 1
        story["dispatch_attempts"] = attempts
        if attempts >= DISPATCH_MAX_ATTEMPTS:
            story["status"] = "failed"
            story["dispatch_error"] = (
                f"agent produced no output in {attempts} launch attempts"
            )
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} failed to launch {attempts}x; "
                f"giving up - needs human intervention.",
            )
            _atomic_write_json(manifest_path, manifest)
            return {"status": "failed", "pid": pid}
        story["status"] = "interrupted"
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid}

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
    last_log_line = _last_nonempty_line(agent_log) if agent_log.exists() else ""  # noqa: F821

    # Infra-failure exit routing: a dispatch that died on an LLM/Ollama
    # transport error (after chat()'s own retries and the 5xx trim-retry are
    # exhausted) is not a review/test-quality outcome and must not be graded
    # or counted against rework_attempts - see INFRA_FAILURE_LOG_SUBSTRING's
    # docstring for the live incident this fixes.
    if INFRA_FAILURE_LOG_SUBSTRING in last_log_line:
        sha = _commit_wip(str(worktree), story_key, "infra_failure")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _store.append_journal(  # noqa: F821
            plan_name,
            story_key,
            {
                "step": "infra_failure",
                "summary": "Dispatch died on an infrastructure failure (LLM/Ollama "
                "transport error); checkpointed for resume.",
                "next_hint": "",
                "commit": sha,
                "ts": interrupted_at,
            },
        )
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

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
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} dispatch died on an infrastructure failure "
                f"(LLM/Ollama transport error) on {current_model}; resuming "
                f"from the last checkpoint.",
            )

        fallback_model = manifest.get("local_model_fallback")
        if (
            fallback_model
            and current_model != fallback_model
            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
            and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
        ):
            story["model"] = fallback_model
            story.pop("infra_failure_streak", None)
            story.pop("infra_failure_streak_model", None)
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; switching to "
                f"fallback model {fallback_model} for the next resume.",
                story_key=story_key,
                event="model_fallback",
                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
            )
        elif (
            not fallback_model
            and _auto_escalation_enabled()  # noqa: F821
            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
            and not story.get("escalated")
            and _escalation_target() != (story.get("backend"), story.get("model"))
            and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
        ):
            _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; escalating to "
                f"{_escalation_label()} (no local_model_fallback configured).",
                story_key=story_key,
                event="escalated",
                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
            )
            return {
                "status": "todo",
                "reason": "infra_failure_escalated_to_claude",
                "pid": pid,
            }
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "infra_failure"}

    if last_log_line in STEP_CAP_MARKERS:
        sha = _commit_wip(str(worktree), story_key, "step_cap_reached")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _store.append_journal(  # noqa: F821
            plan_name,
            story_key,
            {
                "step": "step_cap_reached",
                "summary": "Agent hit the step cap; checkpointed for resume.",
                "next_hint": "",
                "commit": sha,
                "ts": interrupted_at,
            },
        )
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

        # CLAUDE.md Step 9: diagnose where this implementer struggled and fold
        # the root cause into agent_instructions so the resume isn't a blind
        # retry. Runs before any model-switch/escalation below so the diagnosis
        # uses the model that just ran (the struggling one) and the worktree's
        # agent.log is still present for evidence. Fail-open: a None/errored
        # diagnosis leaves agent_instructions untouched (no-op).
        _rebrief_step_cap_struggle(  # noqa: F821
            story, str(worktree), plan_role_config=manifest.get("role_config"),
            plan_name=plan_name,
            story_key=story_key)
        # Worktree hygiene: append the cleanup-guidance section so the resumed
        # agent tidies the worktree before continuing. Unconditional and
        # idempotent (append_cleanup_guidance is a no-op when its header is
        # already present), so it composes safely with the diagnosis above.
        story["agent_instructions"] = append_cleanup_guidance(
            story.get("agent_instructions", ""))

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
            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
        ):
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                story["model"] = fallback_model
                story.pop("step_cap_streak", None)
                story.pop("step_cap_streak_model", None)
                _notify_user(  # noqa: F821
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; switching to fallback model "
                    f"{fallback_model} for the next resume.",
                    story_key=story_key,
                    event="model_fallback",
                    **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
                )
        elif (
            not fallback_model
            and _auto_escalation_enabled()  # noqa: F821
            and story.get("backend", "local") in _LOCAL_BACKEND_NAMES
            and not story.get("escalated")
            and _escalation_target() != (story.get("backend"), story.get("model"))
        ):
            # No local_model_fallback opt-in for this plan: under auto
            # dispatch, escalate to Claude instead of cycling on the same
            # struggling local model forever. Mutually exclusive with the
            # local-fallback branch above (gated on `not fallback_model`) -
            # no chaining from local fallback to Claude.
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
                _notify_user(  # noqa: F821
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; escalating to {_escalation_label()} (no "
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
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}

    # The read-only oracle is described as read-only in prompt text only;
    # this is the mechanism behind it. Refuse tests_passed on a worktree whose
    # acceptance fixture no longer matches its dispatch-time digest, so a
    # rewritten grader is caught at the done-bar rather than only much later
    # at the merge gate (see pipeline.ci._acceptance_tampered).
    tampered = _acceptance_tampered(story, str(worktree))
    if tampered:
        story["status"] = "changes_requested"
        _notify_user(  # noqa: F821
            plan_name,
            f"{story_key} acceptance fixture modified since dispatch: "
            f"{', '.join(tampered)} - refusing tests_passed",
        )
        _atomic_write_json(manifest_path, manifest)
        return {
            "status": "changes_requested",
            "reason": "acceptance_tampered",
            "tampered": tampered,
        }

    test_dir, test_cmd = detect_test_command(worktree)  # noqa: F821

    # FM-A: when the story carries an acceptance block, gate on only those
    # oracle test files rather than the full worktree suite. The model's own
    # tests can contain wrong assertions (the "graded on own buggy tests"
    # failure mode); the harness-owned oracle is the authoritative bar.
    # _scope_test_cmd_to_acceptance scopes pytest (path args), cargo
    # (--test <stem>), and npm/yarn-with-node --test; other runners fall back
    # to the whole suite (the MBW safety net — a story without an acceptance
    # block, or a runner we can't safely scope, still gets the full re-run).
    #
    # Paths are materialized relative to the worktree root (dispatch_story),
    # but test_dir can be a child subdirectory when the buildable project
    # doesn't live at the worktree root (detect_test_command's fallback).
    # Use absolute paths so the scoped run works regardless of test_dir.
    acceptance = story.get("acceptance") or []
    # A rework redispatch is held to a full-suite-green done-bar (dispatch.py
    # raises it on the agent side under exactly this condition). Re-applying the
    # FM-A acceptance scoping here would grade the same round on a narrower bar
    # than the one the agent was held to, so a round the agent could not green
    # would land on tests_passed anyway - fail-open (observed live 2026-09-21:
    # the oracle parked at the suite-reject cap with the branch tip unchanged
    # and the repo-wide suite red, and the tick still promoted the story off the
    # scoped fixture run).
    rework_full_suite = (
        story.get("backend", "local") in _LOCAL_BACKEND_NAMES
        and bool(story.get("ci_rework") or story.get("review_feedback"))
    )
    if acceptance and not rework_full_suite:
        acceptance_paths = [str(worktree / p) for p in _acceptance_rel_paths(story)]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
        if scoped is not None:
            test_cmd = scoped
    elif _is_pytest_cmd(test_cmd):
        # Mode 42 done-bar blindspot: a story without an acceptance block
        # whose deliverable lives under tests/ can add its own test_*.py
        # there, which an --ignore then hides from this same gate run
        # (see _added_pytest_test_paths). Pass those paths explicitly so the
        # model's own tests for its own tests/-scoped code actually execute.
        #
        # But ONLY the paths that are genuinely hidden. A positional path
        # argument REPLACES pytest's default collection rather than adding
        # to it, so appending an already-collected path silently converts
        # this full-suite done-bar into a single-file done-bar:
        #
        #   pytest --override-ini=testpaths=. --ignore=tests/benchmark
        #       -> collects the whole suite
        #   pytest --override-ini=testpaths=. --ignore=tests/benchmark A.py
        #       -> collects ONLY A.py
        #
        # The unconditional append was safe only while
        # _apply_pytest_collection_overrides emitted a blanket
        # `--ignore=tests` (every tests/ path was hidden, so every path
        # needed passing). That blanket ignore was removed - it now emits
        # only --ignore=tests/benchmark --ignore=tests/experiments - which
        # left the append narrowing the gate for the common case.
        #
        # Live consequence (2026-09-02, PR #552): story f68b8350's gate ran
        # only its own new tests/unit/test_guard_liveness_check.py (23
        # tests), never ran the pre-existing sibling test file that was red,
        # returned 0, and merged onto a broken master.
        own_test_paths = _added_pytest_test_paths(
            worktree, story_key, _default_branch()  # noqa: F821
        )
        ignored = _pytest_ignored_paths(test_cmd)  # noqa: F821
        hidden_paths = [
            rel for rel in own_test_paths
            if _is_hidden_by_pytest_ignores(rel, ignored)  # noqa: F821
        ]
        if hidden_paths:
            test_cmd = [*test_cmd, *(str(worktree / p) for p in hidden_paths)]

    # Grade in a clean dev env, not the MCP server's operational one. The
    # server carries PIPELINE_* (pause/resume thresholds, backend dispatch,
    # model defaults) so advance_pipeline/check_usage see the real config —
    # but those same vars override the defaults the test suite asserts
    # against (e.g. usage_gate thresholds, dispatch backend routing) and
    # false-fail the gate for every Python story. Strip them so the suite
    # sees the same defaults a developer runs it under.
    #
    # Also strip LOCAL_AGENT_* and REPO_ROOT: LOCAL_AGENT_* (read-heavy
    # windows, chat retry, etc.) are harness-config the scheduler's plist may
    # set for a run (e.g. LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS raised to
    # let a model explore longer), and test_local_agent.py asserts the
    # DEFAULTS — an override that survives into the graded run false-fails
    # the suite for every story in a repo that vendors the pipeline's own
    # tests (the dashboard worktree is the pipeline repo, so its full suite
    # includes test_local_agent.py). REPO_ROOT is a per-plan sentinel
    # (/nonexistent-...) that likewise isn't a developer default.
    test_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    # Dead-pid recovery (2026-09-02 live incident): a dispatch whose process
    # is gone used to be re-graded SYNCHRONOUSLY here, inside the scheduler
    # tick - advance.py calls check_story_status for every in_progress story,
    # so one dead story blocked dispatch/review/merge for ALL plans for the
    # duration of a full suite run (~19 min of scheduler silence, observed
    # live). The grade is now handed to a detached wrapper process (adp-01's
    # start_detached_grade) and picked up by a LATER tick, keyed on per-story
    # bookkeeping persisted in the manifest:
    #
    #   no grading_pid    -> spawn the detached grade, record grading_pid /
    #                        grading_started_at / grading_result_path, and free
    #                        the tick immediately.
    #   grading_pid alive -> pure read: still grading, manifest untouched.
    #   grading_pid dead  -> collect the result (fail-closed when the wrapper
    #                        left none) and resume the post-grade logic below
    #                        through a subprocess.run-shaped shim; the
    #                        bookkeeping is consumed so a future re-grade
    #                        starts clean.
    _early, test_result = _detached_grade_lifecycle(
        story, story_key, pid, worktree, manifest, manifest_path,
        test_cmd, test_dir, test_env,
    )
    if _early is not None:
        return _early

    if test_result is None:
        # Heavy build/test commands (cargo, npm, mvn, gradle, etc.) can run GB-
        # seconds of memory each. Serialize against other in-flight agents so
        # we never have N concurrent builds saturating the host. Cheap commands
        # (pytest, mvn, gradle, make, npm — depending on the project) skip the
        # lock entirely.
        if _is_heavy(test_cmd):  # noqa: F821
            with _heavy_lock():
                test_result = subprocess.run(
                    test_cmd,
                    check=False,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    env=test_env,
                )
        else:
            test_result = subprocess.run(
                test_cmd,
                check=False,
                cwd=test_dir,
                capture_output=True,
                text=True,
                env=test_env,
            )
    passed = test_result.returncode == 0
    if not passed:
        exempted = _baseline_exempted_failures(story, test_result)
        if exempted:
            # This red run failed only on node ids the pre-dispatch baseline
            # was already failing before this story touched the worktree, so
            # the grade must not reject the story for them. Recorded, never
            # silent: the exempted ids stay on the manifest for audit.
            passed = True
            story["grade_baseline_exemption"] = {
                "failed_node_ids": exempted,
                "baseline_returncode": story["baseline_test_check"]["returncode"],
                "ts": datetime.now(timezone.utc).isoformat(),
            }

    # Diagnostic gap found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD):
    # this test-run result was only ever returned transiently from the tool
    # call - nothing persisted it, so a status that later turned out to be
    # wrong (tests_passed recorded when the same command deterministically
    # fails on manual re-run) was impossible to diagnose after the fact.
    # Persist it on the story every time, regardless of pass/fail, so a
    # future occurrence leaves a paper trail. getattr() on stderr: some
    # test doubles for subprocess.run's return value don't define it.
    # The worktree's current HEAD sha is recorded alongside so later
    # dispatch/review/rebrief logic can detect when this cache is stale
    # (recorded at a past commit) and refuse to reuse it.
    check_sha = _record_test_check(story, test_cmd, test_dir, test_result, worktree)
    if passed:
        lint = _run_lint_gate(worktree, test_env)  # noqa: F821
        if lint is not None:
            lint["sha"] = check_sha
            story["last_lint_check"] = lint
            if lint["returncode"] != 0:
                passed = False

    # Only worth checking once the baseline (tests, lint) actually passed -
    # a story already failing on those has enough signal without this too.
    if passed:
        dead_functions = _find_dead_new_functions(worktree, _default_branch())  # noqa: F821
        story["last_dead_code_check"] = dead_functions
        if dead_functions:
            passed = False

    # The agent produced real output and the tests ran: the launch worked, so
    # clear any failed-launch attempts accumulated by earlier infra blips.
    # The step-cap, infra-failure and watchdog streaks go too: all gate
    # escalation on CONSECUTIVE failures, and a dispatch that got this far
    # breaks any streak. Without this, non-consecutive failures accumulated
    # across a story's whole life (two infra deaths early, one much later,
    # real progress in between) would escalate as though they were
    # consecutive.
    _clear_failure_streaks(story)

    # A red grade confined to pre-existing test files the branch never touched
    # is a PLAN CONFLICT, not a failed attempt: the brief and those tests
    # contradict each other, so the overlord role rules on it. Returning here,
    # before the status/rework assignment below, is what keeps a conflict from
    # being charged as a failed attempt. The helper resolves through globals()
    # for the same reason as _untrack_scratchpad: this body's globals ARE
    # pipeline.server's namespace, so the probe sees the pytest-conditional
    # export (absent under pytest, where every pre-existing grade test pins
    # subprocess.run's call sequence). The test result and the streaks above
    # are recorded first: a security-relevant ruling must still leave a
    # test-run record on the manifest.
    if not passed:
        _intercept = globals().get("_plan_conflict_intercept")
        if _intercept is not None:
            _conflict = _intercept(
                plan_name, story_key, story, test_result, worktree,
                manifest, manifest_path, pid,
            )
            if _conflict is not None:
                return _conflict

    # False-positive guard: tests passing against an untouched worktree
    # (e.g. main's suite against an empty branch because the agent parked
    # in a repetition loop without writing code) is not "the task is done."
    # require at least one commit on the agent branch beyond the base
    # branch before we count it as `tests_passed`. Mark `failed` (not
    # `interrupted`) because re-dispatching the same prompt to the same
    # model on the same empty worktree is unlikely to produce a different
    # outcome next tick; better to surface it for the dashboard.
    if passed and not _worktree_has_new_commits(  # noqa: F821
        worktree,
        story_key,
        base_branch=_default_branch(),  # noqa: F821
    ):
        base = _default_branch()  # noqa: F821
        story["status"] = "failed"
        story["failure_reason"] = (
            f"tests passed but agent branch has no new commits vs {base}; "
            "agent likely parked without writing code."
        )
        _atomic_write_json(manifest_path, manifest)
        return {"status": "failed", "reason": "empty_agent_branch"}

    story["status"] = "tests_passed" if passed else "failed"

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
        and _worktree_has_new_commits(  # noqa: F821
            worktree, story_key, base_branch=_default_branch()  # noqa: F821
        )
    ):
        story["status"] = "tests_passed"  # reviewable; reviewer sees the failure
        story["acceptance_failed_review"] = True

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
    if story["status"] == "tests_passed" and story.get("last_reviewed_sha"):
        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if head_res.stdout.strip() == story["last_reviewed_sha"]:
            attempts = story.get("rework_attempts", 0) + 1
            story["rework_attempts"] = attempts
            # Zero commits since the last review is a stronger, distinct
            # signal from "made changes but the reviewer wasn't satisfied" -
            # see REWORK_MAX_ATTEMPTS_NO_COMMIT's docstring in config.py.
            # Applies uniformly regardless of escalation/oracle status.
            rework_cap = REWORK_MAX_ATTEMPTS_NO_COMMIT
            if attempts >= rework_cap:
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
                if _auto_escalation_enabled() and not story.get("escalated"):  # noqa: F821
                    _escalate_review_to_claude(
                        story,
                        story_key,
                        plan_name,
                        f"no new commit after {attempts} rework redispatches",
                    )
                    story["status"] = "changes_requested"
                    _atomic_write_json(manifest_path, manifest)
                    return {
                        "status": "changes_requested",
                        "reason": "no_new_commit_escalated_to_claude",
                    }
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"no new commit after {attempts} rework redispatches - "
                    "agent keeps parking/crashing without writing code."
                )
                _atomic_write_json(manifest_path, manifest)
                _notify_user(  # noqa: F821
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
            # LD90 W6: resuming the same transcript after a round that wrote
            # nothing replays the confusion that produced it. Drop the
            # transcript so the next rework starts cold from the brief at the
            # last commit, and fold a root-cause diagnosis into the brief
            # (same fail-open rebrief the step-cap path uses).
            (worktree / ".agent_transcript.json").unlink(missing_ok=True)
            _rebrief_step_cap_struggle(  # noqa: F821
                story, str(worktree), plan_role_config=manifest.get("role_config"),
                plan_name=plan_name, story_key=story_key)
            story["status"] = "changes_requested"
            _atomic_write_json(manifest_path, manifest)
            return {
                "status": "changes_requested",
                "reason": "no_new_commit_since_last_review",
            }

    # T6: distinguish an explicit agent surrender from an ordinary red test
    # run. A missing/wrong API is a story-scoping bug, not a model-capability
    # gap - the terminal notify in advance_pipeline uses this to point a
    # human at "clarify the story" instead of the generic "tests failed".
    give_up_summary = _last_done_summary(agent_log) if not passed else ""  # noqa: F821
    if give_up_summary and _is_give_up_summary(give_up_summary):
        story["failure_kind"] = "give_up"
    else:
        story.pop("failure_kind", None)

    _atomic_write_json(manifest_path, manifest)

    result = {
        "status": story["status"],
        "tests_passed": passed,
        "test_command": test_cmd,
        "output_tail": test_result.stdout[-500:],
    }
    if story.get("failure_kind"):
        result["failure_kind"] = story["failure_kind"]
    return result


def bind_to_server(_server: Any) -> None:
    """Rebind check_story_status and export its collaborators onto ``_server``.

    Called by pipeline.server right after it imports this module, so this module
    itself never imports the server and can be imported cold."""
    global check_story_status, _detached_grade_lifecycle

    # Rebind the function's globals to pipeline.server's namespace so that
    # bare-name reads inside the body (e.g. `detect_test_command`,
    # `_worktree_has_new_commits`, `_store`) resolve against pipeline.server at
    # call time. This preserves the original behavior where the function lived in
    # pipeline.server and saw monkeypatched module globals (LOAD_GLOBAL does not
    # consult a module-level __getattr__, so a plain re-export would not).
    check_story_status = types.FunctionType(
        check_story_status.__code__,
        _server.__dict__,
        check_story_status.__name__,
        check_story_status.__defaults__,
        check_story_status.__closure__,
    )
    # check_story_status's own logic (not a primitive): rebound onto pipeline.server's
    # globals so its globals().get(...) probes see the same pytest-conditional exports
    # check_story_status sees, and exported unconditionally.
    _detached_grade_lifecycle = types.FunctionType(
        _detached_grade_lifecycle_unbound.__code__,
        _server.__dict__,
        _detached_grade_lifecycle_unbound.__name__,
        _detached_grade_lifecycle_unbound.__defaults__,
        _detached_grade_lifecycle_unbound.__closure__,
    )
    _server._detached_grade_lifecycle = _detached_grade_lifecycle

    # The rebound body resolves bare names against pipeline.server's namespace, so
    # the grading watchdog constant it reads must be reachable there. (The
    # detached-grade primitives are exported further down, after their defs.)
    _server.DETACHED_GRADE_WATCHDOG_SECONDS = DETACHED_GRADE_WATCHDOG_SECONDS
    _server.DISPATCH_STALE_ACTIVITY_SECONDS = DISPATCH_STALE_ACTIVITY_SECONDS
    _server.collect_story_wedge_signals = collect_story_wedge_signals


    # The rebound check_story_status body resolves bare names against
    # pipeline.server's namespace, so the detached-grade primitives it calls must
    # be reachable there. Export them into _server.__dict__ at import time — but
    # NOT under pytest: the pre-existing check_story_status test files pin the
    # SYNCHRONOUS dead-pid grade (they stub subprocess.run and never stub these
    # primitives), and exporting the real spawner under pytest would flip those
    # runs to detached grades and break them. The detached-grading tests patch
    # p.start_detached_grade / p.collect_detached_grade directly — exactly the
    # surface the rebound body reads via globals().
    # The rebound check_story_status body resolves bare names against
    # pipeline.server's namespace, so the helper its verdict block calls must
    # be reachable there - same reason the detached-grade primitives below are
    # exported.
    _server._baseline_exempted_failures = _baseline_exempted_failures

    # The rebound body's test-result bookkeeping and streak clearing are pure
    # manifest mutations (the same ones it performed inline before they were
    # extracted), so they are exported unconditionally: the grade path must record
    # its test run and clear its streaks on every outcome, including a
    # plan-conflict verdict.
    _server._record_test_check = _record_test_check
    _server._clear_failure_streaks = _clear_failure_streaks

    # The post-agent scratchpad untrack has the detached-grade primitives'
    # reason AND one of its own: it shells out to git, so exporting the real
    # function under pytest would run it inside every pre-existing test that
    # drives the dead-pid grade path (they stub subprocess.run and pin its call
    # sequence). The untrack wiring tests patch p._untrack_scratchpad directly -
    # the surface the rebound body reads via globals(). The plan-conflict
    # intercept is exported on the same terms: it shells out to git and calls the
    # overlord role, so under pytest it stays absent from pipeline.server and
    # every pre-existing check_story_status test keeps its exact subprocess.run
    # call sequence.
    if "pytest" not in sys.modules:
        _server.start_detached_grade = start_detached_grade
        _server.collect_detached_grade = collect_detached_grade
        _server._untrack_scratchpad = _untrack_scratchpad
        _server._plan_conflict_intercept = _plan_conflict_intercept
