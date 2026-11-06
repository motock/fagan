"""The ``story.review_start`` audit event: when a review began, not when it ended.

The audit stream brackets a story's life with ``story.dispatch`` and
``story.merge`` but had no marker for a review starting, so time-in-review
could not be computed from the stream afterwards (liveness S9). This module
appends that one marker and computes nothing.

Two deliberate asymmetries in the record:

* it is written AFTER the review call returns, because the review may skip -
  ``unchanged_since_last_review``, ``not_reviewable_state``, ``locked`` - and a
  skip is not a review. The record's own ``ts`` is therefore the END of the
  call, so ``detail["started_at"]`` carries the start captured before it. For a
  detached review (a ``review_job`` handle) that start is also as much as can
  be known here: the event marks when review was started, not when the verdict
  landed.
* it carries identifiers and the two timing fields only - no verdict, diff,
  feedback, path or sha.

Recording must never endanger the review it describes: the caller's result is
always returned untouched, because a review must not fail because its audit line
could not be written. Two log paths, and they differ:

* an append that fails with an ``OSError`` - a read-only plan directory, a full
  disk - is already swallowed inside :func:`pipeline.audit.record_audit`, which
  logs it once at ERROR with the action and plan and a full traceback;
* only an *unexpected* non-``OSError`` reaches this module's ``except``, which
  logs one WARNING by exception type alone (a message can carry a story key,
  which has no place in an operator log).

A call that is not a review writes nothing: a non-dict result, a ``skipped``
key - ``unchanged_since_last_review``, ``not_reviewable_state``, ``locked`` -
or ``ok is False``. A ``deferred`` result (``rate_limited``,
``transient_backend``, ``backend_error``) is a review that was started and
backed off, so it does record one.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from datetime import UTC, datetime
from typing import Any

from .audit import record_audit

logger = logging.getLogger(__name__)

#: The audit action this module appends.
ACTION = "story.review_start"


def now() -> float:
    """The review-start clock, kept here so callers import no extra name."""
    return time.time()


def _iso_utc(ts: float) -> str:
    """Render *ts* as UTC ISO-8601 at seconds precision.

    Explicit ``tz=UTC`` matters: ``datetime.fromtimestamp`` without it silently
    uses the machine's local zone, which would put ``started_at`` hours ahead of
    the record's ``ts`` on non-UTC hosts and break "started no later than
    recorded". Seconds precision keeps it comparable and stable.
    """
    return (
        datetime.fromtimestamp(ts, tz=UTC).replace(microsecond=0).isoformat()
    )


def audited_review(fn):
    """Record a ``story.review_start`` event around a ``review_story`` method.

    The start is captured BEFORE the wrapped call and the event is emitted
    after it returns, so a skipped or locked review writes nothing and
    ``detail["started_at"]`` can never be later than the record's ``ts``.

    The wrapped method is called exactly as before - same arguments, same lock
    handling, same force path - and its result is returned untouched: an audit
    failure is swallowed inside :func:`record_review_started`.
    """

    # Computed once at decoration time, not on every call.
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # Bind against the undecorated signature so the plan, story and force
        # are read without consuming anything the wrapped call needs.
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        started = now()
        result = fn(*args, **kwargs)
        record_review_started(
            arguments.get("plan_name"),
            arguments.get("story_key"),
            result,
            started,
            arguments.get("force", False),
        )
        return result

    return wrapper


def record_review_started(
    plan_name: str,
    story_key: str,
    result: Any,
    started: float,
    force: bool = False,
) -> None:
    """Append one ``story.review_start`` record for a review that really ran.

    Emits nothing when *result* is not a dict, carries a ``skipped`` key (a
    skip is not a review), or reports ``ok is False``. A ``deferred`` result is
    a review that started and backed off, so it does emit one.

    Never raises. An ``OSError`` from the append never even arrives here - it is
    swallowed and logged at ERROR inside :func:`pipeline.audit.record_audit`.
    Only an unexpected non-``OSError`` reaches this ``except``, which logs one
    WARNING by exception type alone: the exception message can carry a story
    key, which has no place in an operator log.

    ``detail["started_at"]`` is *started* (captured before the review call);
    the record's own ``ts`` is the end of the call.
    """
    try:
        # Test key PRESENCE, not truthiness: a skip reports the reason in it.
        if not isinstance(result, dict) or "skipped" in result:
            return
        if result.get("ok") is False:
            return
        record_audit(
            plan_name,
            action=ACTION,
            outcome="started",
            target={"plan": plan_name, "story": story_key},
            detail={"started_at": _iso_utc(started), "forced": bool(force)},
        )
    except Exception as exc:  # noqa: BLE001 - an audit line must never fail a review
        logger.warning("review start audit failed: %s", type(exc).__name__)
