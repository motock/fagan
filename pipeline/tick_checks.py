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

from pipeline import manifest_source, outbox_health, paths, persistence
from pipeline.consumer_watchdog import ConsumerWatchdog

logger = logging.getLogger(__name__)

#: Only the NATS bus has durable consumers, so only it has stalls to report.
EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"
NATS_BUS = "nats"

#: Whether the last tick saw a stale outbox. Kept here, next to the other
#: per-tick check state, so the notice fires once per transition rather than
#: once per tick: ``True`` after ``outbox_stale``, reset by ``outbox_recovered``.
_OUTBOX_STALE = False

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


def _first_plan_name() -> str | None:
    """Name of the first plan, from the record store when there is one.

    Platform-wide notices have no plan of their own, so they are attributed to
    the first plan as a stopgap (the alert is operator-audience, not that plan's
    — see enterprise ADR 0001). "First" is the sorted-first name, so the same
    deployment keeps attributing to the same plan instead of following whatever
    order the filesystem happens to return.

    The plans are enumerated by :func:`pipeline.manifest_source.read_plan_names`
    — the one store-first listing, next to the one store-first manifest read —
    rather than globbed here: the store-selection rule (lazy
    ``pipeline.server._store`` lookup, legacy :class:`pipeline.store.FileStore`
    excluded) then lives in exactly one place. This module passes the
    ``paths.PLAN_DIR`` binding it documents as patchable, so a patched binding
    is honoured on the file path *and* the tests keep their fixture directory.

    ``fallback_on_error`` is ``True``: attribution is advisory, like the rest of
    the tick, so a store that cannot list its plans is logged at debug level and
    degrades to the file. ``None`` means "no plan to attribute to yet" rather
    than an error - and nothing here raises, because :func:`_notify_transition`
    calls this outside its own guard.
    """
    try:
        names = manifest_source.read_plan_names(
            fallback_on_error=True, plans_root=paths.PLAN_DIR
        )
    except Exception:  # a notice must never break the tick
        logger.debug("first-plan attribution failed", exc_info=True)
        return None
    return names[0] if names else None


def _notify_transition(durable: str, transition: str) -> None:
    """Tell the user about one stall transition, or skip if we cannot route it.

    Attribution is :func:`_first_plan_name`: the notice goes to the first plan
    the record store (or this host) knows. That is a stopgap — the alert is
    operator-audience, not that plan's — so it is flagged for the tenancy work.
    """
    # TENANCY: platform notice attributed to first plan; see enterprise ADR 0001
    plan_name = _first_plan_name()
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


def _notify_outbox(event: str, summary: dict) -> None:
    """Tell the user the outbox stalled, or that it is draining again.

    Attribution is :func:`_first_plan_name`: the notice goes to the first plan
    the record store (or this host) knows. The outbox is instance-wide, so that
    plan owns a platform-wide alert that is not really its own -- which is why
    the same stopgap is flagged for the tenancy work.
    """
    # TENANCY: platform notice attributed to first plan; see enterprise ADR 0001
    plan_name = _first_plan_name()
    if plan_name is None:
        return
    if event == "outbox_stale":
        message = (
            f"The notification outbox is not draining: {summary['records']} "
            f"record(s) in {summary['files']} file(s), oldest "
            f"{summary['oldest_age_s']}s old. Notifications are not being "
            "delivered; check the notification channel and the drain."
        )
        severity = "error"
    else:
        message = "The notification outbox is draining again."
        severity = "info"
    try:
        persistence._notify_user(
            plan_name,
            message,
            event=event,
            severity=severity,
            dedup_key=event,
        )
    except Exception:
        logger.exception("outbox staleness notification failed")


def _check_outbox() -> None:
    """Probe the outbox and emit one notice per staleness transition.

    The probe runs outside any lock the drain needs: :mod:`pipeline.outbox_health`
    only opens files for reading, and the summary is computed before anything
    touches the notifier.
    """
    global _OUTBOX_STALE

    from pipeline.store import _ServerRef
    from pipeline.store_records import DEFAULT_NAMESPACE

    _store = _ServerRef("_store")
    summary = _store.records().outbox_summary(DEFAULT_NAMESPACE)
    event, stale = outbox_health.stale_transition(
        _OUTBOX_STALE,
        summary["oldest_age_s"],
        outbox_health.outbox_stale_seconds(),
    )
    # Store the new flag FIRST: the next tick's decision depends on it, and an
    # unset flag would re-fire the same notice tick after tick.
    _OUTBOX_STALE = stale
    # An unknown age (unreadable spool) is not a recovery: claiming the outbox
    # "is draining again" would be a false all-clear, so only an age we could
    # actually measure may clear the stall.
    if event == "outbox_recovered" and summary["oldest_age_s"] is None:
        return
    if event is not None:
        _notify_outbox(event, summary)


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

    # Advisory: the outbox depth/age verdict has the same fail-soft contract.
    try:
        _check_outbox()
    except Exception:
        logger.exception("outbox staleness check failed")

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
