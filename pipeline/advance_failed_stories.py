"""Advance tick phase: poll the in-progress stories and act on failures.

Moved out of ``pipeline.advance._advance_pipeline_locked_impl``: the
``for key, story in stories.items():`` loop that polls each running agent.
A story that finished ``failed`` is escalated, retried on a fallback model,
rebriefed after a give-up, or reported as a failed test run. The tick calls
this while it holds the plan lock.

Names that tests monkeypatch on ``pipeline.advance`` are read as
``_home.<name>`` at call time, so those patches keep landing here.
"""

import logging
from typing import Any

from . import advance as _home
from . import bound_records
from .dispatch import _resolve_dispatch_target
from .role_routing_scope import layer_routing_for_manifest, layer_routing_kwargs


def poll_running_stories(
    plan_name: str,
    manifest: dict[str, Any],
    stories: dict[str, Any],
    summary: dict[str, Any],
    *,
    dispatch_ok: bool,
    memory_pressure: bool,
) -> None:
    """Poll each in-progress story that survived this tick's interruption gate.

    Appends to ``summary``'s ``failed`` and ``notify`` lists as failures are
    handled. ``manifest`` may be re-read on escalation; the loop continues with
    the re-read copy, as the tick always has.
    """
    for key, story in stories.items():
        if story["status"] != "in_progress" or "pid" not in story:
            continue
        if not _poll_gate_open(
            manifest,
            story,
            dispatch_ok=dispatch_ok,
            memory_pressure=memory_pressure,
        ):
            continue
        check_result = _home.check_story_status(plan_name, key)
        status = check_result.get("status")
        if status == "failed":
            manifest = _handle_failed_story(
                plan_name, key, story, manifest, check_result, summary
            )


def _poll_gate_open(
    manifest: dict[str, Any],
    story: dict[str, Any],
    *,
    dispatch_ok: bool,
    memory_pressure: bool,
) -> bool:
    """Per-story poll gate — mirrors the interruption gate in the tick: poll
    only stories that survive this tick (were NOT interrupted).

    tag falls back to dispatched_model (see _story_dispatch_is_
    on_device): a story with no plan-authored `model` override
    still resolves to a concrete tag at dispatch time, and reading
    only `model` silently dropped every such :cloud dispatch into
    the blanket-gate branch below, starving it of polling whenever
    the blanket gate read down (live incident: PUB-01 finished and
    exited but was never re-polled, so its dead pid was never
    graded).
    """
    story_backend, _story_model = _resolve_dispatch_target(
        story,
        plan_role_config=manifest.get("role_config"),
        **layer_routing_kwargs(layer_routing_for_manifest(manifest)),
    )
    tag = story.get("model") or story.get("dispatched_model") or _story_model
    if story_backend != "claude" and tag and tag.endswith(":cloud"):
        return True  # :cloud is never interrupted by the local memory gate
    if story_backend == "claude" or not tag:
        # Interrupted when the blanket gate is down for a non-memory
        # reason; polled when the gate is up OR down for memory pressure.
        return dispatch_ok or memory_pressure
    # On-device with an explicit model tag: polled only when its own
    # floor holds. Memory pressure alone no longer interrupts it, so do
    # not infer interruption from a low floor here.
    status = _home.backend.get_backend("dispatch", name=story_backend).resource_status(
        model_tag=tag
    )
    return status.get("ok", True)


def _handle_failed_story(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    check_result: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Escalate, fall back, give up or report one story whose tests failed.

    Returns the manifest the tick continues with: an escalation or fallback
    re-reads it, and later stories in the poll must see that copy.
    """
    fallback_model = manifest.get("local_model_fallback")
    # A-posteriori escalation: under auto dispatch, if the
    # local agent failed and has NOT been escalated before,
    # wipe its worktree and re-queue for Claude. A second
    # failure (on Claude), or any failure under an explicit
    # non-auto backend, is terminal.
    if (
        _home._auto_escalation_enabled()
        and story.get("backend") in _home._LOCAL_BACKEND_NAMES
        and not story.get("escalated")
        and _home._escalation_target(
            **layer_routing_kwargs(layer_routing_for_manifest(manifest))
        )
        != (story.get("backend"), story.get("model"))
    ):
        manifest = _home._fresh_manifest(plan_name)
        _home._escalate_to_claude(manifest, plan_name, key)
        _home._notify_user(
            plan_name,
            f"{key} local agent failed; escalating to {_home._escalation_label(**layer_routing_kwargs(layer_routing_for_manifest(manifest)))} and starting clean.",
            event="escalated",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        summary["notify"].append(key)
    elif (
        fallback_model
        and story.get("backend") in _home._LOCAL_BACKEND_NAMES
        and story.get("model") != fallback_model
        and not story.get("tried_fallback_model")
    ):
        # Plan-scoped opt-in (manifest["local_model_fallback"]):
        # never escalates to Claude - just gives one other
        # local model a shot before the terminal park/fail
        # path below.
        manifest = _home._fresh_manifest(plan_name)
        failed_model = story.get("dispatched_model") or story.get("model") or "default"
        _home._escalate_to_local_fallback_model(
            manifest, plan_name, key, fallback_model
        )
        _home._notify_user(
            plan_name,
            f"{key} local agent failed on {failed_model}; retrying on "
            f"fallback model {fallback_model} before parking.",
            event="model_fallback",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        summary["notify"].append(key)
    elif check_result.get("failure_kind") == "give_up":
        _handle_give_up(plan_name, key, story, manifest, summary)
    else:
        _home._notify_user(
            plan_name,
            f"{key} tests failed",
            event="tests_failed",
            story_key=key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        summary["failed"].append(key)
        summary["notify"].append(key)
    return manifest


def _handle_give_up(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """Report an explicit surrender and rebrief the story for its next dispatch."""
    # T6: the agent explicitly surrendered rather than
    # producing ordinary red tests. Point the human at
    # the story's scope/clarity instead of the generic
    # message - a missing/wrong API needs a fix to
    # agent_instructions, not another identical retry.
    _home._notify_user(
        plan_name,
        f"{key} agent gave up (explicit surrender, zero productive "
        f"progress) - likely under-specified (missing API, wrong "
        f"scope) rather than a model-capability gap; needs human "
        f"clarification before another dispatch.",
        event="agent_gave_up",
        story_key=key,
        **(
            {"correlation_id": story["correlation_id"]}
            if story.get("correlation_id")
            else {}
        ),
    )
    summary["failed"].append(key)
    summary["notify"].append(key)
    _rebrief_after_give_up(plan_name, key, story, manifest)


def _rebrief_after_give_up(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """OA2-08: run the step-cap rebrief machinery on a surrendered story.

    A surrender is the same "the brief, not the model, is the problem"
    signal the step-cap path already diagnoses (pipeline/story_status.py's
    _rebrief_step_cap_struggle). Running the SAME rebrief machinery here
    means the next dispatch carries a rewritten brief instead of the
    identical one that just failed. Fail-open: any error (unconfigured/
    raising diagnosis role, unreadable manifest) degrades to the notify-only
    behaviour - a diagnosis must never crash the tick.
    """
    try:
        previous_instructions = story.get("agent_instructions", "")
        evidence = _home.collect_failure_evidence(story.get("worktree", ""), story)
        unsat_reason = _home.detect_unsatisfiable_signal(evidence)
        if unsat_reason is not None:
            _home._notify_user(
                plan_name,
                f"Story may be unsatisfiable as specified: "
                f"{unsat_reason}. Story {key} may need "
                f"re-planning rather than another retry.",
                story_key=key,
            )
        diagnosis = _home.diagnose_failure(
            evidence,
            story,
            manifest.get("role_config"),
            **layer_routing_kwargs(layer_routing_for_manifest(manifest)),
        )
        story["agent_instructions"] = _home.compose_rebriefed_instructions(
            story.get("agent_instructions", ""), diagnosis
        )
        # Persist onto a FRESH read: check_story_status already
        # wrote this story's terminal status/failure_kind, so
        # writing the tick's stale top-of-loop `manifest` back
        # would revert it to in_progress. Only the rewritten
        # brief is carried over.
        fresh = _home._fresh_manifest(plan_name)
        fresh["stories"][key]["agent_instructions"] = story["agent_instructions"]
        bound_records.put_manifest(_home._store, plan_name, fresh)
        if story["agent_instructions"] != previous_instructions:
            _home._notify_user(
                plan_name,
                f"{key} brief rewritten from the give-up "
                f"diagnosis; the re-dispatch carries the "
                f"folded diagnosis.",
                story_key=key,
                event="give_up_rebrief",
                **(
                    {"correlation_id": story["correlation_id"]}
                    if story.get("correlation_id")
                    else {}
                ),
            )
    except Exception:  # fallback: notify-only, never crash the tick
        logging.getLogger("pipeline").debug(
            "give_up rebrief diagnosis failed for %s; falling back to notify-only",
            key,
            exc_info=True,
        )
