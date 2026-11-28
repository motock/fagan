"""Phase functions behind ``SchedulerDaemon.run_once``.

Moved out of ``pipeline/scheduler_daemon.py`` without behaviour change. Each
phase takes the daemon (``d``) and touches the same attributes the inline code
did. Tests patch names on ``pipeline.scheduler_daemon`` (clock helpers, the
timeout readers, ``time``, ``threading``, ``logger``), so every such name is
looked up on that module at call time through ``_home``, never copied.
"""
import logging

from pipeline import scheduler_daemon as _home

# Same logger name as the daemon so records keep their ``pipeline.scheduler_daemon`` name.
logger = logging.getLogger("pipeline.scheduler_daemon")


def run_scan_phase(d) -> bool:
    """Scan phase: always attempted; returns whether the scan completed.

    The scan runs in a worker thread bounded by a join deadline (story sh-02)
    so a wedged scan costs one tick instead of the whole loop.
    """
    scanned = False
    try:
        completed, outcome = d._scan_with_watchdog(d._scan_fn)
        if completed:
            scanned = True
        else:
            elapsed_s = outcome
            logger.error(
                "scan_fn stalled past the %.1fs join deadline "
                "(PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS); abandoning the "
                "worker after %.1fs and continuing the loop",
                _home._scan_join_timeout_seconds(),
                elapsed_s,
            )
            d._scan_timed_out += 1
            d._last_scan_timeout_ts = _home.time.time()
            d._last_error = (
                f"scan_fn stalled past the join deadline "
                f"({elapsed_s:.1f}s elapsed); worker abandoned"
            )
    except Exception as exc:  # pragma: no cover - exercised via tests
        logger.exception("scan_fn raised during scheduler iteration")
        d._last_error = str(exc)
    finally:
        # Update health metrics for scan.
        d._scan_count += 1
        d._last_scan_ts = _home._dt.datetime.now(_home._dt.UTC).isoformat()
        # Write health at the END of the scan phase rather than only at the
        # end of the tick: the later phases can each hold a tick open for
        # minutes, and refreshing here keeps scan_count live.
        if d._health_path is not None:
            try:
                d.write_health(d._health_path)
            except Exception:  # pragma: no cover - must never kill the loop
                logger.exception("write_health failed after scan phase")
    return scanned


def reconcile_is_due(d, now: float) -> bool:
    """True once ``interval_s`` has elapsed since the last reconcile.

    Boundary tolerance: ``now - d._last_reconcile`` is a difference of two
    large monotonic readings, and float rounding of ``(t0 + interval) - t0``
    can land epsilon BELOW ``interval`` even when a full interval has elapsed
    (observed on CI: a 60s advance measured as 59.99999999999909, which
    silently skipped the reconcile phase). Compare against
    ``interval_s - epsilon``: ~1e-9 is far below any real interval, so a
    genuinely shorter elapsed still skips.
    """
    return now - d._last_reconcile >= d._interval_s - max(
        1e-9, abs(d._interval_s) * 1e-12
    )


def run_reconcile_phase(d, now: float) -> bool:
    """Reconcile phase (caller checked it is due); returns whether it completed."""
    reconciled = False
    # Capture the deadline once: the timeout branch below must log the
    # deadline that was actually enforced, not re-read mutable env state.
    timeout_s = _home._reconcile_join_timeout_seconds()
    # This attempt starts clean: the additive last_reconcile_timeout_ts must
    # reflect only the most recent attempt, never an earlier tick's timeout.
    d._last_reconcile_attempt_timed_out = False
    try:
        completed, outcome = d._run_with_watchdog(
            d._reconcile_fn,
            timeout_s=timeout_s,
            label="reconcile",
        )
        if completed:
            reconciled = True
        else:
            elapsed_s = outcome
            logger.error(
                "reconcile_fn stalled past the %.1fs join deadline "
                "(PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS); "
                "abandoning the worker after %.1fs and continuing "
                "the loop",
                timeout_s,
                elapsed_s,
            )
            d._reconcile_timed_out += 1
            d._last_reconcile_timeout_ts = _home.time.time()
            d._last_reconcile_attempt_timed_out = True
            d._last_error = (
                f"reconcile_fn stalled past the join deadline "
                f"({elapsed_s:.1f}s elapsed); worker abandoned"
            )
    except Exception as exc:  # pragma: no cover - exercised via tests
        logger.exception("reconcile_fn raised during scheduler iteration")
        d._last_error = str(exc)
    finally:
        # Update health metrics for reconcile regardless of success.
        d._reconcile_count += 1
        d._last_reconcile_ts = _home._dt.datetime.now(_home._dt.UTC).isoformat()
        d._last_reconcile = now
    return reconciled


def _drain_outbox_worker() -> None:
    """Body of the drain thread.

    Exceptions are caught here, not in the caller's handler: this runs on the
    drain worker, so a raise would reach that thread's excepthook. Module
    attributes are read at call time so patches apply; drains are isolated.
    """
    from pipeline import notification_email, notification_outbox

    try:
        notification_outbox.drain_outbox(
            notification_outbox.ALL_PLANS,
            notification_email.send_notification_email,
        )
    except Exception:
        logger.exception("notification outbox drain failed during tick")
    try:
        notification_outbox.drain_channel_outboxes()
    except Exception:
        logger.exception("instance outbox drain failed during tick")


def _drain_with_bounded_join(d) -> None:
    if d._drain_worker is not None and d._drain_worker.is_alive():
        # A previous tick's worker was abandoned by the join timeout below and
        # is STILL running (a wedged SMTP send, say). A second drainer would
        # race it: ``_drain_one_outbox``'s merge assumes its snapshot is a
        # prefix of the current file, so the second one could silently drop a
        # freshly spooled record. Skip this tick; the next one picks it up.
        logger.warning(
            "previous notification outbox drain worker is still "
            "alive; skipping this tick's drain to avoid two "
            "concurrent drainers racing the outbox merge"
        )
        return
    drain_worker = _home.threading.Thread(
        target=_drain_outbox_worker,
        name="scheduler-drain-worker",
        daemon=True,
    )
    # Remember the worker BEFORE joining it: the join below may abandon it
    # while it is still alive, and the next tick needs this reference to skip
    # rather than overlap the drain.
    d._drain_worker = drain_worker
    drain_worker.start()
    drain_worker.join(_home._drain_join_timeout_seconds())
    if drain_worker.is_alive():
        # A wedged drain (an unreachable SMTP host, say) must not hold the tick
        # open. Deliberately NOT routed through _run_with_watchdog: a slow
        # drain is not evidence of a leaked plan lock, and feeding it into
        # _consecutive_abandons would let an SMTP outage trip the
        # abandon-restart SystemExit and bounce the scheduler.
        logger.warning(
            "notification outbox drain exceeded %.1fs; abandoning the "
            "worker and continuing the tick",
            _home._drain_join_timeout_seconds(),
        )


def run_drain_phase(d) -> None:
    """Drain phase (PLANNOTIFY-06): its own try/except so a drain failure can
    never kill the loop. Imports are function-local to avoid pulling in
    smtplib at module import.
    """
    try:
        from pipeline import notification_email, notification_outbox  # noqa: F401

        _drain_with_bounded_join(d)
    except Exception:
        logger.exception("notification outbox drain failed during tick")


def run_intake_phases() -> None:
    """Jira, GitHub Issues and operator-plugin intake, each in its own
    try/except so a failure never affects the tick. Only the exception type is
    logged; intake text and tokens never reach a log.
    """
    try:
        from pipeline import jira_intake

        jira_intake.run_jira_intake(_home.time.time())
    except Exception as exc:  # noqa: BLE001 - a Jira failure never breaks the tick
        logger.warning(
            "jira intake failed during tick",
            extra={"error_type": type(exc).__name__},
        )

    try:
        from pipeline import github_issues_intake

        github_issues_intake.run_github_issues_intake(_home.time.time())
    except Exception as exc:  # noqa: BLE001 - a GitHub failure never breaks the tick
        logger.warning(
            "github issues intake failed during tick",
            extra={"error_type": type(exc).__name__},
        )

    try:
        from pipeline import intake_plugins

        intake_plugins.run_intake_plugins(_home.time.time())
    except Exception as exc:  # noqa: BLE001 - a plugin failure never breaks the tick
        logger.warning(
            "intake plugins failed during tick",
            extra={"error_type": type(exc).__name__},
        )


def _write_health_or_log(d) -> None:
    if d._health_path is not None:
        try:
            d.write_health(d._health_path)
        except Exception:  # pragma: no cover - unlikely but safe
            logger.exception("write_health failed")


def _exit_for_leaked_workers(d, leaked, grace_s: float) -> None:
    names = ", ".join(
        f"{worker.name!r} (alive {elapsed:.0f}s past its abandonment)"
        for worker, elapsed in leaked
    )
    d._last_error = (
        f"abandoned watchdog worker(s) {names} still alive past the "
        f"{grace_s:.0f}s grace window: the plan _plan_lock held by "
        "the wedged call is leaked and process death is the only "
        "thing that releases the flock — exiting so launchd "
        "restarts the daemon"
    )
    logger.error("%s", d._last_error)
    # Write the health file FIRST so the final state (including the
    # ``abandoned_workers_alive`` count) is observable on disk, THEN exit.
    _write_health_or_log(d)
    raise SystemExit(1)


def _warn_waiting_workers(d, now: float, grace_s: float) -> None:
    for worker, ts in d._abandoned_workers:
        warned_at = d._abandoned_worker_warned_at.get(worker)
        if warned_at is not None and now - warned_at < _home._ABANDON_WARNING_INTERVAL_S:
            # cops-03: this worker's wait was already announced within the
            # last 30s; the final grace-exit ERROR is unchanged.
            continue
        d._abandoned_worker_warned_at[worker] = now
        logger.warning(
            "abandoned watchdog worker %r still alive %ds after its "
            "abandonment; waiting out the %.0fs grace window before "
            "treating its plan _plan_lock as leaked (%.0fs of grace "
            "remaining)",
            worker.name,
            now - ts,
            grace_s,
            grace_s - (now - ts),
        )


def check_abandoned_workers(d) -> None:
    """Prune finished abandoned workers, then exit if a survivor has outlived
    the grace window (a leaked plan ``_plan_lock``: only process death
    releases it). Runs BEFORE the streak-reset/threshold block so the grace
    exit wins over the threshold exit.
    """
    # SRR-2: the abandoned-worker ledger is the streak's missing half.
    d._abandoned_workers = [
        (worker, ts) for worker, ts in d._abandoned_workers if worker.is_alive()
    ]
    # cops-03: prune the warning throttle in step with the ledger.
    d._abandoned_worker_warned_at = {
        worker: warned_at
        for worker, warned_at in d._abandoned_worker_warned_at.items()
        if worker.is_alive()
    }
    grace_s = _home._abandon_worker_grace_seconds()
    now = d._clock()
    # Boundary tolerance: ``now - ts`` is a difference of two large monotonic
    # readings, and float rounding can land epsilon ABOVE ``grace`` even when
    # the elapsed time is exactly the grace window. A worker exactly at the
    # grace is NOT past it, so compare against ``grace_s + epsilon``.
    grace_boundary = grace_s + max(1e-9, abs(grace_s) * 1e-12)
    leaked = [
        (worker, now - ts)
        for worker, ts in d._abandoned_workers
        if now - ts > grace_boundary
    ]
    if leaked:
        _exit_for_leaked_workers(d, leaked, grace_s)
    _warn_waiting_workers(d, now, grace_s)


def finish_tick(d, streak_at_start: int) -> None:
    """Reset the abandonment streak, write health, then apply the
    abandon-restart escape hatch (story LOCKSTARVE-C2).

    A tick in which no phase abandoned a worker means every phase completed
    normally, so the streak ends. The reset happens BEFORE the threshold check
    (and before the health write, so the file reflects the reset): the
    threshold is re-read from the env per call, and checking it first could
    exit on a healthy tick after an operator lowered it under a streak.
    SRR-2: the reset only fires when the pruned ledger is empty, since a live
    abandoned worker keeps its leaked lock and the hatch must stay armed.
    """
    if not d._abandoned_workers and d._consecutive_abandons == streak_at_start:
        d._consecutive_abandons = 0

    _write_health_or_log(d)

    # The exit comes AFTER the health write so the final state is observable
    # on disk, and raises SystemExit (never a hard kill, which would skip the
    # write). ``>=`` rather than ``==``: a tick can abandon more than one
    # worker's worth of streak (e.g. a threshold lowered mid-streak).
    threshold = _home._abandon_restart_threshold()
    if threshold > 0 and d._consecutive_abandons >= threshold:
        logger.error(
            "%d consecutive watchdog worker abandonment(s) reached the "
            "abandon-restart threshold of %d (%s); a leaked plan "
            "_plan_lock held by an abandoned worker is the suspected "
            "cause, and process death is the only thing that releases "
            "the flock — exiting so launchd restarts the daemon",
            d._consecutive_abandons,
            threshold,
            _home._ABANDON_RESTART_THRESHOLD_ENV,
        )
        raise SystemExit(1)
