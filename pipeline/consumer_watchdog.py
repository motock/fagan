"""Consumer-stall watchdog: turn consumer snapshots into liveness transitions.

A JetStream durable consumer whose backlog is non-empty while no client is
fetching it is *stalled*: messages sit undelivered and the pipeline silently
stops making progress. This module turns a stream of
``pipeline.events_nats_consumer.consumer_status()`` snapshots into the two
transitions an operator needs — ``consumer_stalled`` when the condition has
persisted past a threshold, and ``consumer_recovered`` once it clears — and
nothing else. It has no side effects: emitting the notice is the caller's job
(``pipeline.tick_checks``), which keeps this class trivially testable with an
injected clock.

STATE IS IN MEMORY ONLY. A daemon restart forgets every stall, so a stall that
is still in progress when the process restarts is announced a second time once
the threshold elapses again. That is deliberate: durable on-disk state would
have to be reconciled against broker restarts and clock skew for a notice the
operator can safely see twice, and re-announcing one stall is far cheaper than
silently swallowing it. A recovery is only announced for a stall this process
announced itself.

Threshold: ``PIPELINE_CONSUMER_STALL_SECONDS``, re-read on every
:meth:`ConsumerWatchdog.observe` call so an operator can tune it without a
restart. Missing, non-integer, zero and negative values all mean
:data:`DEFAULT_STALL_SECONDS`, so a typo can never turn every tick into a
notice.
"""

from __future__ import annotations

import os

#: Seconds a consumer must be stalled before the first notice. Also the
#: fallback for a missing, malformed or non-positive env value.
DEFAULT_STALL_SECONDS = 300

#: Env var naming the stall threshold; read on every ``observe`` call.
STALL_SECONDS_ENV = "PIPELINE_CONSUMER_STALL_SECONDS"


def stall_threshold_seconds() -> int:
    """Return the effective stall threshold, reading the env afresh.

    Missing, unparsable (``"abc"``, ``"3.5"``, ``""``) and non-positive
    (``"0"``, ``"-5"``) values all fall back to
    :data:`DEFAULT_STALL_SECONDS` rather than failing or firing immediately.
    """
    raw = os.environ.get(STALL_SECONDS_ENV)
    if raw is None:
        return DEFAULT_STALL_SECONDS
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return DEFAULT_STALL_SECONDS
    if value <= 0:
        return DEFAULT_STALL_SECONDS
    return value


class _Stall:
    """Per-durable stall bookkeeping."""

    __slots__ = ("announced", "stall_started_at")

    def __init__(self, stall_started_at: float) -> None:
        self.stall_started_at = stall_started_at
        self.announced = False


class ConsumerWatchdog:
    """Emit one stall notice and one recovery notice per stall episode.

    Call :meth:`observe` once per scheduler tick with the current
    ``consumer_status()`` payload and the tick's ``now`` (seconds; an injected
    value keeps this testable). The return value is a list of
    ``("consumer_stalled" | "consumer_recovered", durable_name)`` tuples,
    usually empty.

    A durable is stalled when ``pending > 0`` and ``subscribed is False``; the
    clock starts on the first tick that shows it and does not move again until
    the episode ends, so a backwards ``now`` cannot postpone the notice and an
    unreachable broker cannot restart the wait. ``{"reachable": False}`` and
    entries missing ``pending`` or ``subscribed`` emit nothing and leave the
    state untouched — an unknown state is not evidence of progress.
    """

    def __init__(self) -> None:
        self._stalls: dict[str, _Stall] = {}

    def observe(self, status: dict, now: float) -> list[tuple[str, str]]:
        """Return the transitions implied by the *status* snapshot at *now*."""
        if not isinstance(status, dict) or status.get("reachable") is False:
            return []
        threshold = stall_threshold_seconds()
        transitions: list[tuple[str, str]] = []
        for durable, entry in status.items():
            if not isinstance(entry, dict):
                continue
            pending = entry.get("pending")
            subscribed = entry.get("subscribed")
            if isinstance(pending, bool) or not isinstance(pending, int):
                continue
            if not isinstance(subscribed, bool):
                continue
            state = self._stalls.get(durable)
            if pending > 0 and subscribed is False:
                if state is None:
                    state = _Stall(now)
                    self._stalls[durable] = state
                if state.announced:
                    continue
                if now - state.stall_started_at >= threshold:
                    state.announced = True
                    transitions.append(("consumer_stalled", durable))
                continue
            # Not stalled: pending drained or a client is fetching again.
            if state is None:
                continue
            del self._stalls[durable]
            if state.announced:
                transitions.append(("consumer_recovered", durable))
        return transitions
