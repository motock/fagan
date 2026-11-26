"""Advance tick phase: review the tests_passed stories of one plan.

Moved out of ``pipeline.advance._advance_pipeline_locked_impl``: the review
step at the end of the tick. When the review backend is up, every
``tests_passed`` story is reviewed; when it is gated, the operator is told
why review is waiting. The tick calls this while it holds the plan lock; the
``_released_plan_lock`` window around each ``review_story`` call is the one
the loop opened before the move.

Names that tests monkeypatch on ``pipeline.advance`` are read as
``_advance().<name>`` at call time, so those patches keep landing here.
"""

import importlib
from typing import Any

from . import review_job
from .review_gate_notice import clear_review_gate_notice, should_notify_review_gate


def _advance():
    """Return ``pipeline.advance`` at call time: it imports this module at load."""
    return importlib.import_module("pipeline.advance")


def run_review_step(
    plan_name: str,
    stories: dict[str, Any],
    summary: dict[str, Any],
    *,
    review_ok: bool,
    review_reason: str,
) -> None:
    """Review ``tests_passed`` stories, or surface the review gate.

    Appends to ``summary``'s ``advanced``, ``review_deferred``, ``skipped``
    and ``notify`` lists.
    """
    # Review every tests_passed story (incl. ones orphaned by a crashed
    # review on a prior tick - review_story is idempotent). Gated by the
    # REVIEW backend independently of dispatch: a Claude-dispatch pause no
    # longer blocks reviewing already-finished work on a healthy review
    # backend, and a local-dispatch run can still defer review if review
    # is on Claude and Claude is gated.
    if review_ok:
        clear_review_gate_notice(plan_name)
        stories = _advance()._fresh_manifest(plan_name)["stories"]
        _review_passed_stories(plan_name, stories, summary)
    else:
        # Only surface the review gate when there is tests_passed work
        # waiting to be reviewed this tick; otherwise the notification is
        # noise (e.g. all stories are already pr_open awaiting merge).
        if any(s["status"] == "tests_passed" for s in stories.values()):
            if should_notify_review_gate(plan_name):
                _advance()._notify_user(
                    plan_name,
                    f"Review backend gated ({review_reason}): deferring review.",
                    dedup_key=f"review_gate:{plan_name}:{review_reason}",
                )
            summary["notify"].append("review_paused")


def _review_passed_stories(
    plan_name: str, stories: dict[str, Any], summary: dict[str, Any]
) -> None:
    # LOCKSTARVE-B4: release the plan lock around review_story, the
    # same synchronous model-call-heavy pattern LOCKSTARVE-B3 applied
    # to dispatch_story. No dispatch lease is needed here -
    # review_story (pipeline/review_orchestrator.py) re-reads the
    # manifest itself and is a documented no-op skip for any story
    # not in "tests_passed", so a second tick/process entering it
    # concurrently cannot corrupt the manifest, and cannot
    # sequentially double-review a story already advanced past
    # tests_passed. It does not rule out two calls racing the
    # SAME simultaneous check-pass window - review_story's own
    # guard bounds that outcome, not this loop.
    #
    # But the per-iteration STATUS CHECK must still come from a
    # FRESH on-disk read, never the snapshot taken before the loop -
    # an earlier iteration's own released-lock window can let
    # another actor already review and advance a later story in
    # this same snapshot. Checking the stale in-memory status would
    # call review_story on it again anyway: review_story's own guard
    # makes that safe from a data-corruption standpoint, but it
    # still emits a spurious "review skipped" notification and a
    # misleading summary["advanced"] entry claiming this tick
    # advanced a story it did not touch. Only the set of keys to
    # consider is safe to snapshot; the status of each must be
    # re-read per iteration.
    for key in list(stories.keys()):
        fresh_story = _advance()._fresh_manifest(plan_name)["stories"].get(key)
        if fresh_story is None:
            continue
        # The detached path also sees a story an operator moved out of
        # tests_passed while its review job was live: that job must be
        # cancelled. review_or_collect starts a job only for a
        # tests_passed story, so passing a moved story on is safe.
        if review_job.detached_enabled() and (
            fresh_story["status"] == "tests_passed" or fresh_story.get("review_job")
        ):
            _advance().review_or_collect(plan_name, key, fresh_story, summary)
            continue
        if fresh_story["status"] != "tests_passed":
            continue
        with _advance()._released_plan_lock(plan_name):
            rv = _advance().review_story(plan_name, key)
        if rv.get("ok") is False or rv.get("skipped"):
            # ok:False - a concurrent re-ingest dropped the story
            # during the released window (review_story returns
            # {"ok": False, "error": ...}, no "status" key).
            # skipped - review_story's own plan-flock guard
            # (_plan_lock) could not take the plan lock during the
            # window this loop deliberately opened (the {"ok": True,
            # "skipped": "locked"} shape, also no "status" key), so no
            # review ran. Either way there is no status to report and
            # rv["status"] would KeyError the whole tick; record the
            # skip and let a later tick retry - the story is still
            # tests_passed on disk. Same handling LOCKSTARVE-B3
            # applies to dispatch_story's contention return.
            summary.setdefault("skipped", []).append(key)
            continue
        summary["advanced"].append({key: rv["status"]})
        if rv.get("deferred") == "rate_limited":
            summary["review_deferred"].append(key)
