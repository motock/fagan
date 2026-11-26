"""Process-spawn phase carved out of ``pipeline.dispatch``.

Holds the tail of ``_dispatch_story_impl``: the pre-spawn fence, the launch of
the agent subprocess with the correlation env staged, and the bookkeeping that
records the pid, dispatch timestamp and dispatched instructions on the story.

The caller holds ``_plan_lock`` and the manifest write happens here at the
same point relative to the launch as before; nothing here touches the lock.
Names tests patch on ``pipeline.dispatch`` are reached through that module at
call time.
"""

import os
from pathlib import Path
from typing import Any


def spawn_agent(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    branch: str,
    worktree_path: Path,
    log_path: Path,
    dispatch_backend: str,
    dispatch_kwargs: dict[str, Any],
    *,
    spec_model: str,
    executor_is_resume: bool,
) -> dict[str, Any]:
    """Launch the agent, record the dispatch and return the result dictionary."""
    from . import dispatch as _home

    # W4-logging slice: hand the agent subprocess the story's correlation
    # ID so its agent.log lines join the orchestrator's events; the drivers
    # build the child env from os.environ at launch time
    # (app/backend_ollama.py's `{**os.environ, ...}` and
    # app/backend_claude.py's _first_party_claude_env), so the var is
    # staged in os.environ for exactly this synchronous launch call and
    # restored afterwards; dispatches are serialized per plan by the plan
    # flock, so no concurrent launch of the same plan observes a torn value.
    # Pre-spawn fence: every slow phase above is done, so re-check for
    # positive evidence that this dispatch must not launch an agent - the
    # worker was abandoned by the scheduler's watchdog, another live
    # process now owns the lease, or the story already reached a terminal
    # status. Two agents in one worktree is the documented cause of
    # zero-output deaths; missing evidence never withholds the spawn.
    fence_error = _home.executor_fence(plan_name, story_key, _home._store)
    if fence_error is not None:
        return fence_error
    prev_correlation_env = os.environ.get("PIPELINE_CORRELATION_ID")
    if story.get("correlation_id"):
        os.environ["PIPELINE_CORRELATION_ID"] = story["correlation_id"]
    prev_traceparent = os.environ.get("TRACEPARENT")
    traceparent = _home.agent_traceparent(story.get("correlation_id"), story.get("dispatch_attempts", 0))
    if traceparent is not None:
        os.environ["TRACEPARENT"] = traceparent
    try:
        handle = _home.backend.get_backend("dispatch", name=dispatch_backend).dispatch(
            **dispatch_kwargs
        )
    finally:
        os.environ.pop("TRACEPARENT", None)
        if prev_traceparent is not None:
            os.environ["TRACEPARENT"] = prev_traceparent
        if prev_correlation_env is None:
            os.environ.pop("PIPELINE_CORRELATION_ID", None)
        else:
            os.environ["PIPELINE_CORRELATION_ID"] = prev_correlation_env

    story["status"] = "in_progress"
    story.pop("conflict_handoff", None)
    story["pid"] = handle.pid
    story["dispatched_at"] = _home.datetime.now(_home.UTC).isoformat()
    story["worktree"] = str(worktree_path)
    story["log"] = str(log_path)
    # Record the concrete model the agent actually boots with (the local
    # backend resolves a logical tier like "sonnet" to e.g.
    # "minimax-m3:cloud"). The dashboard shows this instead of the plan's
    # declared story["model"] so what's displayed matches what ran. The
    # declared tier is left untouched (it's a routing hint).
    if getattr(handle, "model", None):
        story["dispatched_model"] = handle.model
    # Snapshot the agent_instructions this dispatch actually handed the
    # agent, so the next rework redispatch can diff against it to detect
    # an operator's patch_story edit (see revised_instructions_note
    # in the prompt build). Recorded on every dispatch - cold or resumed, any
    # backend - so the baseline is always current regardless of how the next
    # rework is routed.
    story["_dispatched_agent_instructions"] = story.get("agent_instructions", "")
    _home.bound_records.put_manifest(_home._store, plan_name, manifest)

    # W4-logging slice: stamp the success event with the story's
    # correlation ID (plus the role/model context already in scope here)
    # so dispatch's own events join the same trace as the agent
    # subprocess's PIPELINE_CORRELATION_ID. Published straight onto the
    # process bus — make_event stamps correlation_id at TOP level (the
    # W4L-01 kwarg). Lazy imports mirror persistence._notify_user so a
    # monkeypatched event_wiring.get_bus is honored; the wired bus has no
    # "agent_dispatched" handler, so this is a no-op there.
    from .event_wiring import get_bus
    from .events import make_event

    get_bus().publish(
        make_event(
            "agent_dispatched",
            plan_name,
            story_key=story_key,
            payload={
                "role": story.get("role"),
                "model": spec_model,
                "backend": dispatch_backend,
                "attempt": story.get("dispatch_attempts", 0),
                "pid": handle.pid,
                "branch": branch,
                **_home.test_scope_event_fields(story),
            },
            correlation_id=story.get("correlation_id"),
        )
    )

    return {
        "ok": True,
        "story_key": story_key,
        "pid": handle.pid,
        "branch": branch,
        "resumed": executor_is_resume,
    }
