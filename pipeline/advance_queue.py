"""Coalescing per-plan advance queue.

A state change enqueues "advance plan X"; requests for the same plan coalesce
while one is already queued (ADR 0011 decision 3). A request that arrives while
the plan is in flight does not duplicate work either: it marks the plan dirty so
the plan is re-queued at the tail once the current advance finishes, behind any
plan that was already waiting.

The queue is in-process state with no persistence. It is empty after a restart;
the reconcile sweep refills it from the manifests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from pipeline.parsers import _validate_key

logger = logging.getLogger(__name__)


class AdvanceQueue:
    """Thread-safe FIFO of plans awaiting an advance, with same-plan coalescing.

    All mutable state is guarded by a single ``threading.Condition``; every
    state read or mutation happens under it and every wake-up uses
    ``notify_all()`` so no waiter can be starved by another one.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queue: deque[str] = deque()
        self._in_flight: set[str] = set()
        self._dirty: set[str] = set()
        self._last_advanced: dict[str, float] = {}
        self._closed = False

    # -- producer side ----------------------------------------------------

    def request(self, plan_name: str, reason: str = "") -> bool:
        """Ask for an advance of *plan_name*.

        Returns ``True`` when the plan was appended to the queue. Returns
        ``False`` when the request was coalesced (the plan is already queued,
        already in flight and now marked dirty, or the queue is closed).
        """
        if not isinstance(plan_name, str):
            raise TypeError("plan_name must be a str")
        try:
            _validate_key(plan_name)
        except ValueError:
            # _validate_key's message echoes the rejected value; never
            # propagate a plan name into an error message.
            raise ValueError("plan_name is not a valid key") from None

        with self._cond:
            if self._closed:
                return False
            if plan_name in self._queue:
                logger.debug(
                    "advance request coalesced into queued plan",
                    extra={"plan": plan_name, "reason": reason},
                )
                return False
            if plan_name in self._in_flight:
                self._dirty.add(plan_name)
                logger.debug(
                    "advance request coalesced onto in-flight plan",
                    extra={"plan": plan_name, "reason": reason},
                )
                return False
            self._queue.append(plan_name)
            self._cond.notify_all()
        logger.info(
            "advance request queued",
            extra={"plan": plan_name, "reason": reason, "queued": True},
        )
        return True

    # -- consumer side ----------------------------------------------------

    def take(self, timeout_s: float | None) -> str | None:
        """Pop the next queued plan and mark it in flight.

        Blocks until a plan is available or *timeout_s* expires (``None`` waits
        forever). Returns ``None`` on timeout or once the queue is closed.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while True:
                if self._closed:
                    return None
                if self._queue:
                    plan_name = self._queue.popleft()
                    self._in_flight.add(plan_name)
                    return plan_name
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()

    def finish(self, plan_name: str) -> None:
        """Record that the advance of *plan_name* ended.

        A plan marked dirty while in flight is re-queued at the TAIL so plans
        that were already waiting keep their place. Finishing a plan that is
        not in flight is a no-op.
        """
        with self._cond:
            if plan_name not in self._in_flight:
                logger.debug(
                    "advance finish for plan not in flight",
                    extra={"plan": plan_name},
                )
                return
            self._in_flight.discard(plan_name)
            self._last_advanced[plan_name] = time.monotonic()
            if plan_name in self._dirty:
                self._dirty.discard(plan_name)
                self._queue.append(plan_name)
            self._cond.notify_all()

    def last_advanced(self, plan_name: str) -> float | None:
        """Return the monotonic time of the last finished advance, if any."""
        with self._cond:
            return self._last_advanced.get(plan_name)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Stop the queue: wake every waiter and reject later requests."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
