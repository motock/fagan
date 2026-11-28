"""Advance tick phase: dispatch the ready stories of one plan.

Moved out of ``pipeline.advance._advance_pipeline_locked_impl``: the per-tick
on-device slot cap, the ``for key in ready:`` dispatch loop and the "dispatch
is waiting" notice. The tick calls this while it holds the plan lock; the
``_released_plan_lock`` window around each ``dispatch_story`` call is the one
the loop opened before the move.

Names that tests monkeypatch on ``pipeline.advance`` are read as
``_advance().<name>`` at call time, so those patches keep landing here.
"""

import importlib
from typing import Any

from . import access_hooks, bound_records
from .concurrency import PlanLockReacquireTimeout, _has_live_test_author_phase
from .dispatch import _resolve_dispatch_target
from .dispatch_fence import blocks_reclaim
from .dispatch_lease import claim_dispatch_lease
from .role_routing_scope import layer_routing_for_manifest, layer_routing_kwargs


def _advance():
    """Return ``pipeline.advance`` at call time: it imports this module at load."""
    return importlib.import_module("pipeline.advance")


def dispatch_ready_stories(
    plan_name: str,
    manifest: dict[str, Any],
    stories: dict[str, Any],
    ready: list[str],
    summary: dict[str, Any],
) -> None:
    """Dispatch ``ready`` stories; record outcomes in ``summary``.

    Appends to ``summary``'s ``dispatched``, ``pending_phase``, ``skipped``,
    ``failed`` and ``notify`` lists, and sets ``dispatch_gated`` when the
    dispatch gate held stories back.
    """
    # 1. Dispatch ready (and resumable-interrupted) stories, capped to
    # the ON-DEVICE slots still free under MAX_CONCURRENT_AGENTS. <=0
    # means no cap. Only on-device dispatch consumes a slot: cloud-backed
    # dispatch (a :cloud-tagged model or a claude-routed story) has no
    # on-device footprint to protect, so it bypasses the cap entirely —
    # its spend is bounded by the usage pause thresholds in the per-story
    # gate below instead. _count_in_progress_agents keeps its backend-
    # blind cross-plan semantics for its other callers
    # (pipeline/dispatch.py's concurrent-dispatch warning gates); only
    # this cap's slot math gets the on-device-aware count.
    if _advance().MAX_CONCURRENT_AGENTS > 0:
        free_device_slots = max(
            0, _advance().MAX_CONCURRENT_AGENTS - _advance()._count_on_device_in_progress_agents()
        )
        capped = True
    else:
        free_device_slots = 0
        capped = False
    # Per-story dispatch gate: only dispatch stories whose own backend+model
    # passes its gate. A :cloud story dispatches even under local memory
    # pressure; an on-device story is deferred when the floor is not met.
    # Independent of the cap: a slot-exempt story still runs this gate,
    # and gated stories are recorded exactly as before.
    gated: list[tuple[str, str | None]] = []
    dispatched_this_tick = 0
    for key in ready:
        if (
            _advance().PIPELINE_MAX_DISPATCH_PER_TICK > 0
            and dispatched_this_tick >= _advance().PIPELINE_MAX_DISPATCH_PER_TICK
        ):
            # Per-tick dispatch cap (LOCKSTARVE-A3): this plan has already
            # launched its budget for this tick. Leave the remaining
            # stories in their current dispatch-eligible status (todo /
            # interrupted) so the NEXT tick picks them up; they are not
            # gated, parked or failed and consume no dispatch attempt.
            break
        story = stories[key]
        on_device = _advance()._story_dispatch_is_on_device(story)
        # A story waiting on a detached test-author already occupies a
        # slot (it is counted above), so re-dispatching it to collect the
        # phase must neither be blocked by nor consume another slot.
        holds_slot = _has_live_test_author_phase(story)
        if capped and on_device and not holds_slot and free_device_slots <= 0:
            # On-device slots are exhausted: defer this story until a
            # slot frees up. Cloud dispatches never reach this branch.
            continue
        if _dispatch_gated(manifest, story, key, gated):
            continue
        claimed = _claim_for_dispatch(plan_name, key)
        if claimed is None:
            continue
        # Persist the claim BEFORE releasing the lock: an unpersisted lease
        # protects nothing.
        bound_records.put_manifest(_advance()._store, plan_name, claimed)
        if capped and on_device and not holds_slot:
            # Only a dispatch that actually launches on-device consumes
            # the slot; a cloud dispatch must not touch the counter.
            free_device_slots -= 1
        if _launch_claimed_story(plan_name, key, summary):
            # Only a dispatch that actually launched consumes this tick's
            # cap budget. Every real dispatch_story result is a dict
            # ({"ok": True, ...} on a launch, {"ok": False, ...} raised as
            # RuntimeError above, {"ok": True, "skipped": ...} on store-lock
            # contention — that last shape did NOT launch an agent but is
            # still counted against the cap, since the tick paid the call),
            # so in production this counts exactly the dispatches that did
            # not return ok: False. A non-dict return (only possible from a
            # stub) is not counted.
            dispatched_this_tick += 1

    # Say why ready stories were held back: the gate reason is otherwise
    # used only for interruptions, so a dead dispatch backend left stories
    # in todo with no notice at all. One notice per distinct reason per
    # gated episode (see _take_new_gate_reasons in pipeline.advance).
    gate_groups = _advance()._group_gated_stories(gated)
    if gate_groups:
        first_reason, first_keys = next(iter(gate_groups.items()))
        summary["dispatch_gated"] = {"reason": first_reason, "stories": first_keys}
    for reason in _advance()._take_new_gate_reasons(plan_name, list(gate_groups)):
        _advance()._notify_user(
            plan_name,
            f"dispatch is waiting: {reason}",
            event="dispatch_gated",
            severity="warning",
            dedup_key=f"dispatch_gated:{plan_name}:{reason}",
        )


def _dispatch_gated(
    manifest: dict[str, Any],
    story: dict[str, Any],
    key: str,
    gated: list[tuple[str, str | None]],
) -> bool:
    """Return True (and record the reason in ``gated``) when the story's own
    backend gate is down, so the tick must not dispatch it this time."""
    story_backend, _story_model = _resolve_dispatch_target(
        story,
        plan_role_config=manifest.get("role_config"),
        **layer_routing_kwargs(layer_routing_for_manifest(manifest)),
    )
    if story_backend == "claude":
        # Gate on Claude's usage resource_status, NOT local memory.
        status = _advance().backend.get_backend("dispatch", name="claude").resource_status()
    else:
        tag = story.get("model") or _story_model
        if tag:
            status = _advance().backend.get_backend(
                "dispatch", name=story_backend
            ).resource_status(model_tag=tag)
        else:
            status = _advance().backend.get_backend(
                "dispatch", name=story_backend
            ).resource_status()
    if status.get("ok", True):
        return False
    gated.append((key, status.get("reason")))
    return True


def _claim_for_dispatch(plan_name: str, key: str) -> dict[str, Any] | None:
    """Claim the dispatch lease on a FRESH read of the story; None to skip it.

    LOCKSTARVE-B3: the check-then-set must run against a fresh read, not the
    top-of-tick story: an earlier iteration already released the lock, so
    another owner may hold this story's lease - claiming stale would miss it
    and clobber it with our own.
    """
    m = _advance()._fresh_manifest(plan_name)
    fresh_story = m["stories"].get(key)
    if fresh_story is None:
        # A re-ingest during an earlier release window can drop this
        # story entirely - skip it rather than KeyError the tick.
        return None
    if fresh_story["status"] not in ("todo", "interrupted", "changes_requested"):
        # Another process already advanced this story during a
        # release window; re-dispatching it would be the same
        # double-dispatch by a different route.
        return None
    if blocks_reclaim(fresh_story, fresh_story.get("worktree") or ""):
        _advance()._notify_user(
            plan_name,
            f"{key}: the previous dispatch's scheduler is gone but its "
            "worktree shows work in progress; waiting for the lease "
            "to expire.",
            event="dispatch_in_progress",
            story_key=key,
        )
        return None
    if not access_hooks.admit_dispatch(fresh_story):
        # Deferred for this tick only: no lease, no slot, not parked.
        return None
    if not claim_dispatch_lease(fresh_story):
        return None
    return m


def _launch_claimed_story(plan_name: str, key: str, summary: dict[str, Any]) -> bool:
    """Run ``dispatch_story`` with the plan lock released.

    Returns True when the call returned a dict, i.e. it counts against this
    tick's dispatch cap. A failed launch is recorded as a retry or a park and
    returns False.
    """
    try:
        with _advance()._released_plan_lock(plan_name):
            result = _advance().dispatch_story(plan_name, key)
        if isinstance(result, dict) and result.get("ok") is False:
            # dispatch_story now returns a structured failure (e.g. a
            # git fetch/worktree-add error) instead of letting the
            # underlying subprocess.CalledProcessError escape as an
            # unhandled exception - re-raise here so the failure handling
            # below (attempt-counting/failed-status/notify-user) still
            # triggers exactly as it did when dispatch_story used to raise
            # directly.
            raise RuntimeError(result.get("error", "dispatch failed"))
        if isinstance(result, dict) and result.get("pending"):
            # Detached test-author still running: no executor launched.
            summary.setdefault("pending_phase", []).append(key)
        elif not (isinstance(result, dict) and result.get("skipped")):
            summary["dispatched"].append(key)
        else:
            summary.setdefault("skipped", []).append(key)
    except PlanLockReacquireTimeout:
        # The plan flock could not be re-acquired after the released
        # window: this thread no longer holds it, so any further
        # manifest mutation this tick makes would race whichever
        # other thread/process took it over. Re-raise rather than
        # letting the broad handler below treat this as an ordinary
        # dispatch failure - that would bump dispatch_attempts and
        # keep writing the manifest with no lock held, and (if the
        # timeout fires after dispatch_story already launched the
        # agent) misattribute a healthy in-progress story as a
        # failed dispatch. Let the tick abort with this cause
        # instead; the next tick starts clean.
        raise
    except Exception as e:  # noqa: BLE001 - recorded by _record_dispatch_failure (git fetch/worktree/backend launch failure)
        _record_dispatch_failure(plan_name, key, e, summary)
        return False
    else:
        # Dispatch succeeded (or was skipped on store-lock contention):
        # clear the lease from a fresh read too, as in the except above.
        _clear_dispatch_lease(plan_name, key)
        return isinstance(result, dict)


def _record_dispatch_failure(
    plan_name: str, key: str, e: Exception, summary: dict[str, Any]
) -> None:
    """Bump the story's attempt counter, park it when attempts are spent."""
    # Re-read: dispatch_story only writes on a successful launch,
    # so on a raise the recorded status is still todo/interrupted -
    # bump the attempt counter there, and clear the lease from the
    # SAME fresh read: the lock was released above, so only a
    # freshly re-read copy may be written back (never a stale one).
    m = _advance()._fresh_manifest(plan_name)
    st = m["stories"][key]
    st.pop("dispatch_lease_expires_at", None)
    st.pop("dispatch_lease_owner_pid", None)
    attempts = st.get("dispatch_attempts", 0) + 1
    st["dispatch_attempts"] = attempts
    # W4L-04: stamp the story's dispatch-failure notifications with
    # its persisted correlation_id (omitted entirely for older
    # manifests). The attempt counter is already in scope here.
    _cid_kwargs = (
        {
            "correlation_id": st["correlation_id"],
            "attempt": st.get("dispatch_attempts", 0),
        }
        if st.get("correlation_id")
        else {}
    )
    if attempts >= _advance().DISPATCH_MAX_ATTEMPTS:
        st["status"] = "failed"
        st["dispatch_error"] = str(e)
        _advance()._notify_user(
            plan_name,
            f"{key} dispatch failed {attempts}x "
            f"({e}); giving up - needs human intervention.",
            event="dispatch_failed",
            story_key=key,
            **_cid_kwargs,
        )
        summary["failed"].append(key)
    else:
        # leave status dispatch-eligible; the next tick retries.
        _advance()._notify_user(
            plan_name,
            f"{key} dispatch attempt {attempts}/"
            f"{_advance().DISPATCH_MAX_ATTEMPTS} failed ({e}); will retry.",
            **_cid_kwargs,
        )
    summary["notify"].append(key)
    bound_records.put_manifest(_advance()._store, plan_name, m)


def _clear_dispatch_lease(plan_name: str, key: str) -> None:
    """Drop the dispatch lease from a fresh read of the story."""
    m = _advance()._fresh_manifest(plan_name)
    st = m["stories"][key]
    st.pop("dispatch_lease_expires_at", None)
    st.pop("dispatch_lease_owner_pid", None)
    bound_records.put_manifest(_advance()._store, plan_name, m)
