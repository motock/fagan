"""Coalescing per-plan advance queue.

A state change enqueues "advance plan X"; requests for the same plan coalesce
while one is already queued (ADR 0011 decision 3). A request that arrives while
the plan is in flight does not duplicate work either: it marks the plan dirty so
the plan is re-queued at the tail once the current advance finishes, behind any
plan that was already waiting.

Entries are keyed by ``(namespace, plan_name)`` (ADR 0012): plan names are unique
only inside a namespace. ``None`` is the process default namespace and is its
own key, never merged with a named one. Namespaces with queued entries take turns
in a rotation, so a busy namespace cannot starve the others.

The queue is in-process state with no persistence. It is empty after a restart;
the reconcile sweep refills it from the manifests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from pipeline.parsers import _validate_key
from pipeline.store_records import validate_namespace

logger = logging.getLogger(__name__)

# (namespace, plan_name); a namespace of None is the process default.
_Entry = tuple[str | None, str]

# Sentinel returned by _wait_locked once the caller's timeout has elapsed.
_TIMED_OUT = object()


def _log_fields(namespace: str | None, plan_name: str, **fields: object) -> dict:
    """Return log ``extra`` fields; ``namespace`` appears only when it is set."""
    extra: dict = {"plan": plan_name, **fields}
    if namespace is not None:
        extra["namespace"] = namespace
    return extra


class AdvanceQueue:
    """Thread-safe fair queue of plans awaiting an advance, with coalescing.

    All mutable state is guarded by a single ``threading.Condition``; every
    state read or mutation happens under it and every wake-up uses
    ``notify_all()`` so no waiter can be starved by another one.

    Invariant: a namespace is in ``_rotation`` exactly when its FIFO in
    ``_fifos`` is non-empty.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._fifos: dict[str | None, deque[str]] = {}
        self._rotation: deque[str | None] = deque()
        self._in_flight: set[_Entry] = set()
        self._dirty: set[_Entry] = set()
        self._last_advanced: dict[_Entry, float] = {}
        self._closed = False

    # -- internal (call with the condition held) ----------------------------

    def _is_queued(self, entry: _Entry) -> bool:
        namespace, plan_name = entry
        return plan_name in self._fifos.get(namespace, ())

    def _enqueue(self, entry: _Entry) -> None:
        namespace, plan_name = entry
        fifo = self._fifos.get(namespace)
        if fifo is None:
            fifo = self._fifos[namespace] = deque()
        if not fifo:
            self._rotation.append(namespace)
        fifo.append(plan_name)

    def _pop_locked(self) -> _Entry:
        """Pop from the rotation head and mark the entry in flight."""
        namespace = self._rotation.popleft()
        fifo = self._fifos[namespace]
        plan_name = fifo.popleft()
        if fifo:
            self._rotation.append(namespace)
        else:
            del self._fifos[namespace]
        entry: _Entry = (namespace, plan_name)
        self._in_flight.add(entry)
        return entry

    def _wait_locked(self, deadline: float | None) -> float | None | object:
        """Wait for a wake-up; return the deadline, or ``_TIMED_OUT`` when spent."""
        if deadline is None:
            self._cond.wait()
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _TIMED_OUT
        self._cond.wait(remaining)
        return deadline

    # -- producer side ----------------------------------------------------

    def request(
        self, plan_name: str, reason: str = "", *, namespace: str | None = None
    ) -> bool:
        """Ask for an advance of *plan_name* in *namespace*.

        Returns ``True`` when the entry was appended to the queue. Returns
        ``False`` when the request was coalesced (the entry is already queued,
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
        if namespace is not None:
            try:
                validate_namespace(namespace)
            except ValueError:
                # validate_namespace is generic, but keep the message fixed
                # so no part of the value can reach an error message.
                raise ValueError("namespace is not valid") from None

        entry: _Entry = (namespace, plan_name)
        with self._cond:
            if self._closed:
                return False
            if self._is_queued(entry):
                logger.debug(
                    "advance request coalesced into queued plan",
                    extra=_log_fields(namespace, plan_name, reason=reason),
                )
                return False
            if entry in self._in_flight:
                self._dirty.add(entry)
                logger.debug(
                    "advance request coalesced onto in-flight plan",
                    extra=_log_fields(namespace, plan_name, reason=reason),
                )
                return False
            self._enqueue(entry)
            self._cond.notify_all()
        logger.info(
            "advance request queued",
            extra=_log_fields(namespace, plan_name, reason=reason, queued=True),
        )
        return True

    # -- consumer side ----------------------------------------------------

    def take_entry(self, timeout_s: float | None) -> _Entry | None:
        """Pop the next entry and mark it in flight.

        Blocks until an entry is available or *timeout_s* expires (``None``
        waits forever). Returns ``None`` on timeout or once the queue is closed.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while True:
                if self._closed:
                    return None
                if self._rotation:
                    return self._pop_locked()
                deadline = self._wait_locked(deadline)
                if deadline is _TIMED_OUT:
                    return None

    def take(self, timeout_s: float | None) -> str | None:
        """Pop the next default-namespace plan and mark it in flight.

        Returns the plan name ``str``, or ``None`` on timeout or once the queue
        is closed. Raises ``RuntimeError`` without popping when the next entry
        belongs to a named namespace; such callers must use :meth:`take_entry`.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while True:
                if self._closed:
                    return None
                if self._rotation:
                    if self._rotation[0] is not None:
                        raise RuntimeError("namespaced entry needs take_entry")
                    return self._pop_locked()[1]
                deadline = self._wait_locked(deadline)
                if deadline is _TIMED_OUT:
                    return None

    def finish(self, plan_name: str, *, namespace: str | None = None) -> None:
        """Record that the advance of *plan_name* in *namespace* ended.

        A pair marked dirty while in flight is re-queued at the TAIL of its
        namespace's FIFO so entries that were already waiting keep their place.
        Finishing a pair that is not in flight is a no-op.
        """
        entry: _Entry = (namespace, plan_name)
        with self._cond:
            if entry not in self._in_flight:
                logger.debug(
                    "advance finish for plan not in flight",
                    extra=_log_fields(namespace, plan_name),
                )
                return
            self._in_flight.discard(entry)
            self._last_advanced[entry] = time.monotonic()
            if entry in self._dirty:
                self._dirty.discard(entry)
                self._enqueue(entry)
            self._cond.notify_all()

    def last_advanced(
        self, plan_name: str, *, namespace: str | None = None
    ) -> float | None:
        """Return the monotonic time of the last finished advance, if any."""
        with self._cond:
            return self._last_advanced.get((namespace, plan_name))

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Stop the queue: wake every waiter and reject later requests."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
