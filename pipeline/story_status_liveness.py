"""Liveness phases of check_story_status: is the dispatched agent still running,
and if it is gone without output, is that a failed launch?

Extracted verbatim from pipeline/story_status.py (behavior-preserving move).
Each phase returns a finished result dictionary, or None meaning "continue".
"""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import bound_records, server_bindings
from .config import STEP_CAP_FALLBACK_THRESHOLD


def check_process_liveness(
    plan_name: str, story_key: str, story: dict[str, Any], manifest: dict[str, Any], pid: int
) -> dict[str, Any] | None:
    """Return the running/watchdog result while the agent process is alive, else None."""
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
            return _alive_result(plan_name, story_key, story, manifest, pid)
        # process is zombie or gone — fall through to test detection
    except ProcessLookupError:
        pass
    return None


def _alive_result(
    plan_name: str, story_key: str, story: dict[str, Any], manifest: dict[str, Any], pid: int
) -> dict[str, Any]:
    dispatched_at = story.get("dispatched_at")
    if dispatched_at is None:
        return {"status": "running", "pid": pid}
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(dispatched_at)).total_seconds()
    # Determine if activity is stale
    signals = server_bindings.server().collect_story_wedge_signals(plan_name, story_key, story)
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
    if activity_age is not None and activity_age > server_bindings.server().DISPATCH_STALE_ACTIVITY_SECONDS:
        watchdog_summary = (
            f"Dispatch watchdog: no activity for {activity_age:.0f}s "
            f"(stale-activity watchdog); elapsed {elapsed:.0f}s; "
            f"process terminated."
        )
        dispatch_error = (
            f"watchdog killed after {activity_age:.0f}s with no activity"
        )
    # Fallback to wall-clock watchdog if activity is unknown or not stale
    elif activity_age is None and elapsed > server_bindings.server().DISPATCH_WATCHDOG_SECONDS:
        watchdog_summary = (
            f"Dispatch watchdog: no completion after {elapsed:.0f}s; process terminated."
        )
        dispatch_error = (
            f"watchdog killed after {elapsed:.0f}s with no completion"
        )
    if watchdog_summary is None:
        # No watchdog trigger
        return {"status": "running", "pid": pid}
    return _watchdog_kill(
        plan_name, story_key, story, manifest, pid, watchdog_summary, dispatch_error
    )


def _watchdog_kill(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
    watchdog_summary: str,
    dispatch_error: str,
) -> dict[str, Any]:
    # OA2-03: every termination path must increment a counter
    # that participates in a streak/cap, or a story that keeps
    # hanging resumes the same struggling model forever.
    # Increment BEFORE the checkpoint so the persisted state
    # carries the new value - incrementing after would lag by
    # one kill and the threshold escalation would never fire.
    story["watchdog_streak"] = story.get("watchdog_streak", 0) + 1
    server_bindings.server()._terminate_and_checkpoint(
        manifest,
        plan_name,
        story_key,
        story,
        pid=pid,
        step="dispatch_watchdog_timeout",
        summary=watchdog_summary,
    )
    server_bindings.server()._notify_user(
        plan_name,
        f"{story_key} killed by the dispatch watchdog; the "
        f"re-dispatch counts as a rework cycle.",
        story_key=story_key,
        severity="warning",
        event="dispatch_watchdog_timeout",
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    server_bindings.server()._rebrief_step_cap_struggle(
        story, str(Path(story["worktree"])),
        plan_role_config=manifest.get("role_config"),
        plan_name=plan_name,
        story_key=story_key)
    story["dispatch_error"] = dispatch_error
    escalated = _watchdog_model_fallback(plan_name, story_key, story, manifest, pid)
    if escalated is not None:
        return escalated
    bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
    return {
        "status": "interrupted",
        "pid": pid,
        "watchdog_killed": True,
    }


def _watchdog_model_fallback(
    plan_name: str, story_key: str, story: dict[str, Any], manifest: dict[str, Any], pid: int
) -> dict[str, Any] | None:
    """Switch model or escalate once the watchdog streak hits the threshold.

    Returns the escalated result (manifest already written) when the story was
    escalated to Claude, else None.
    """
    # OA2-03 convergence: at the threshold, stop resuming the
    # same struggling local model. Produce the SAME
    # fallback/escalation signal the step-cap streak path
    # produces at STEP_CAP_FALLBACK_THRESHOLD - a plain resume
    # here would let a story that keeps hanging loop forever.
    # The counter is deliberately NOT reset on this path: a
    # later kill must keep escalating, not restart the count.
    fallback_model = manifest.get("local_model_fallback")
    current_model = story.get("dispatched_model") or story.get("model")
    if story["watchdog_streak"] < STEP_CAP_FALLBACK_THRESHOLD:
        return None
    if (
        fallback_model
        and current_model != fallback_model
        and story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
    ):
        story["model"] = fallback_model
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} hit the watchdog kill {STEP_CAP_FALLBACK_THRESHOLD}x "
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
        server_bindings.server()._escalate_to_claude(manifest, plan_name, story_key)
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} hit the watchdog kill {STEP_CAP_FALLBACK_THRESHOLD}x "
            f"on {current_model}; escalating to {server_bindings.server()._escalation_label()} (no "
            f"local_model_fallback configured).",
            story_key=story_key,
            event="escalated",
            **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
        )
        bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
        return {
            "status": "todo",
            "reason": "step_cap_escalated_to_claude",
            "pid": pid,
        }
    return None


def check_empty_launch_log(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    pid: int,
    agent_log: Path,
) -> dict[str, Any] | None:
    """Handle an agent whose log is still empty: running within the grace
    window, a counted failed launch after it. Returns None when the log has
    output and the caller should keep checking.
    """
    if not (agent_log.exists() and agent_log.stat().st_size == 0):
        return None
    # Empty log within the startup grace window means the agent is alive
    # and bootstrapping - its first print() hasn't flushed yet, especially
    # when queued on Ollama's -np 1 worker behind another request. The
    # PID-alive check above already passed, so trust that and don't burn
    # dispatch_attempts on a process that's just slow to print. After the
    # grace window elapses with the log still empty, the agent is
    # presumed genuinely dead (failed launch) and we count it.
    log_age = time.time() - agent_log.stat().st_mtime
    if log_age < server_bindings.server().DISPATCH_STARTUP_GRACE_SECONDS:
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
    if attempts >= server_bindings.server().DISPATCH_MAX_ATTEMPTS:
        story["status"] = "failed"
        story["dispatch_error"] = (
            f"agent produced no output in {attempts} launch attempts"
        )
        server_bindings.server()._notify_user(
            plan_name,
            f"{story_key} failed to launch {attempts}x; "
            f"giving up - needs human intervention.",
        )
        bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
        return {"status": "failed", "pid": pid}
    story["status"] = "interrupted"
    bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
    return {"status": "interrupted", "pid": pid}
