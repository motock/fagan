"""Per-tick advisory checks run from the scheduler's health write.

``SchedulerDaemon.write_health`` used to own the "is this checkout stale?"
probe inline. The consumer-stall watchdog is a second, independent advisory
probe on the same cadence, so both live here: :func:`run_tick_checks` is the
one call the daemon makes, and every check inside it is fail-soft. A probe
that raises must never break the health write, and a notification that fails
must never break the tick.

The watchdog only means anything when the pipeline actually runs a JetStream
bus, so the whole consumer side is skipped unless ``PIPELINE_EVENT_BUS`` is
``nats``: on the in-process bus there are no durable consumers to stall, and
probing the broker from a daemon that does not use it would just log noise
(and, on the health path, a connect attempt per tick).
"""

from __future__ import annotations

import logging
import os
import time
import weakref

from pipeline import paths, persistence
from pipeline.consumer_watchdog import ConsumerWatchdog

logger = logging.getLogger(__name__)

#: Only the NATS bus has durable consumers, so only it has stalls to report.
EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"
NATS_BUS = "nats"

#: One watchdog per daemon, so its in-memory stall clock spans ticks. Building
#: a fresh one each tick would restart the clock every tick and never fire.
_WATCHDOGS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _watchdog_for(daemon) -> ConsumerWatchdog:
    """Return *daemon*'s watchdog, creating it on first use."""
    try:
        watchdog = _WATCHDOGS.get(daemon)
        if watchdog is None:
            watchdog = ConsumerWatchdog()
            _WATCHDOGS[daemon] = watchdog
        return watchdog
    except TypeError:
        # Not weak-referenceable: fall back to state on the daemon itself so
        # the clock still spans ticks.
        watchdog = getattr(daemon, "_consumer_watchdog", None)
        if watchdog is None:
            watchdog = ConsumerWatchdog()
            try:
                daemon._consumer_watchdog = watchdog
            except AttributeError:  # pragma: no cover - exotic daemon
                pass
        return watchdog


def _consumer_status(daemon) -> dict:
    """Read the broker's per-durable consumer state.

    ``SchedulerDaemon`` has no probe of its own, so the module-level
    ``consumer_status()`` from the NATS consumer is the normal path; a daemon
    that grows one (a fake in a test, a future daemon that caches the probe)
    wins.
    """
    probe = getattr(daemon, "consumer_status", None)
    if probe is None:
        from pipeline import events_nats_consumer

        probe = events_nats_consumer.consumer_status
    return probe()


def _notify_transition(durable: str, transition: str) -> None:
    """Tell the user about one stall transition, or skip if we cannot route it.

    Attribution mirrors :meth:`SchedulerDaemon._notify_if_checkout_stale`: the
    notice goes to the first manifest plan. That is a stopgap — the alert is
    operator-audience, not that plan's — so it is flagged for the tenancy work.
    """
    # TENANCY: platform notice attributed to first plan; see enterprise ADR 0001
    plan_name = next(
        (
            path.name.removesuffix(".manifest.json")
            for path in sorted(paths.PLAN_DIR.glob("*.manifest.json"))
        ),
        None,
    )
    if plan_name is None:
        return
    if transition == "consumer_stalled":
        message = (
            f"Consumer {durable} has a non-empty backlog and no client fetching "
            "it: pipeline events are not being processed. Check that the "
            "scheduler's consumers are running."
        )
        event, severity = "consumer_stalled", "error"
    else:
        message = f"Consumer {durable} is processing its backlog again."
        event, severity = "consumer_recovered", "info"
    try:
        persistence._notify_user(
            plan_name,
            message,
            event=event,
            severity=severity,
            dedup_key=f"{event}:{durable}",
        )
    except Exception:
        logger.exception("consumer watchdog notification failed for %s", durable)


def run_tick_checks(daemon, config: dict) -> None:
    """Run the advisory per-tick checks for *daemon*. Never raises.

    *config* is the daemon's config fingerprint (what
    ``write_health`` computed), which the staleness check needs;
    ``health()``'s key set is pinned and must not carry it.
    """
    # Advisory: a merged orchestration change stays inert until restart.
    # Never let this check break the health write.
    try:
        daemon._notify_if_checkout_stale(config)
    except Exception:
        logger.exception("scheduler staleness check failed")

    if os.environ.get(EVENT_BUS_ENV) != NATS_BUS:
        return

    try:
        transitions = _watchdog_for(daemon).observe(_consumer_status(daemon), time.time())
    except Exception:
        # Leave the watchdog's state untouched: the stall clock keeps running
        # from when the stall was first seen, so one failed probe does not
        # delay the notice by another full threshold.
        logger.exception("consumer watchdog check failed")
        return

    for transition, durable in transitions:
        _notify_transition(durable, transition)
