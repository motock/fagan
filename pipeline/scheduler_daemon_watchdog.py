"""Bounded-join watchdog and heartbeat for the scheduler daemon's phases.

Moved out of ``pipeline/scheduler_daemon.py`` without behaviour change. The
``SchedulerDaemon`` methods of the same names delegate here, and the daemon's
heartbeat state (``_heartbeat_phase``, ``_consecutive_abandons``, ...) stays on
the daemon instance, so the watchdog reads and writes the same fields.

The logger keeps the ``pipeline.scheduler_daemon`` name: the daemon's log
channel is unchanged by where the code lives.
"""
import datetime as _dt
import logging
import threading
import time

from pipeline.scheduler_timeouts import _heartbeat_interval_seconds

from .actor import SYSTEM_SCHEDULER, bind_principal
from .dispatch_fence import mark_worker_abandoned

logger = logging.getLogger("pipeline.scheduler_daemon")


def join_with_heartbeat(daemon, worker, timeout_s, label):
    """Join *worker* in heartbeat-sized slices, refreshing health between.

    Story SCHED-HEARTBEAT: a single ``worker.join(timeout_s)`` of up to
    900s blocks the main thread with health written only after the phase
    ends, so ``last_scan_ts`` and the health file's mtime freeze while the
    daemon is healthy and busy (the scan phase runs bus handlers
    synchronously — reviews, seconds to minutes). Slicing the join at
    ``PIPELINE_SCHEDULER_HEARTBEAT_SECONDS`` and refreshing health after
    each unfinished slice keeps the file advancing, so a reader can tell a
    busy-but-alive daemon from a dead or wedged one.

    The total deadline is unchanged: the slices sum to *timeout_s*, and
    the caller's ``is_alive()`` check remains the only timed-out-vs-done
    signal. ``_heartbeat_phase`` is set only while the phase is genuinely
    mid-join and cleared when the join returns, so a clean tick's file
    payload stays exactly ``{**health(), "config": ...}``.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        worker.join(min(_heartbeat_interval_seconds(), remaining))
        if not worker.is_alive():
            break
        # The slice elapsed without the worker finishing: the phase is
        # still running, so stamp it and refresh health before the next
        # slice. Guarded exactly like the other health writes — a failing
        # write must never kill the loop.
        daemon._heartbeat_phase = label
        daemon._heartbeat_ts = _dt.datetime.now(_dt.UTC).isoformat()
        if daemon._health_path is not None:
            try:
                daemon.write_health(daemon._health_path)
            except Exception:  # pragma: no cover - must never kill the loop
                logger.exception("write_health failed during %s heartbeat", label)
    daemon._heartbeat_phase = None


def run_with_watchdog(daemon, fn, *, timeout_s, label):
    """Run ``fn`` in one worker daemon-thread with a bounded join.

    Generalisation of the scan-phase watchdog (story LOCKSTARVE-C1) so
    ``reconcile_fn`` gets the same bounded blast radius. Returns
    ``(completed, result)`` where ``completed`` is False when the
    worker outlived the join deadline (and ``result`` is the elapsed
    seconds). A captured exception is re-raised here (on the caller's
    thread) so ``run_once``'s existing swallow path runs unchanged. The
    abandoned worker is never joined again: it is a daemon thread, so it
    can never block interpreter exit, and it dies once its blocked call
    eventually returns.

    KNOWN LIMITATION (do not fix here): abandoning the worker leaves any
    plan ``_plan_lock`` held by the wedged call locked until the process
    dies — the watchdog converts a total freeze into a degraded-but-alive
    scheduler; lock recovery is future work. The escape hatch (story
    LOCKSTARVE-C2) bounds the damage: after
    ``PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD`` consecutive abandonments
    the daemon exits so launchd restarts it and the flock is released.
    """
    box = {}

    def _worker():
        try:
            with bind_principal(SYSTEM_SCHEDULER):
                box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised by caller
            box["error"] = exc

    worker = threading.Thread(
        target=_worker, name=f"scheduler-{label}-worker", daemon=True
    )
    worker.start()
    started = time.monotonic()
    # Thread.join(timeout) ALWAYS returns None; the is_alive() check below
    # is the only correct timed-out-vs-done signal. The join is sliced at
    # the heartbeat interval (story SCHED-HEARTBEAT) so health keeps
    # advancing while a long phase runs; the total deadline is unchanged.
    join_with_heartbeat(daemon, worker, timeout_s, label)
    if not worker.is_alive():
        if "error" in box:
            raise box["error"]
        return True, box.get("result")
    elapsed = time.monotonic() - started
    # Abandon the worker: daemon-flagged, never joined again, dies when
    # its blocked call returns. KNOWN LIMITATION: abandoning it leaves any
    # plan _plan_lock held by the wedged call locked until the process
    # dies; the watchdog converts a total freeze into a degraded-but-alive
    # scheduler, and lock recovery is future work.
    # LOCKSTARVE-C2: count the abandonment toward the consecutive streak
    # that drives the abandon-restart escape hatch. Both phases route
    # through here, so this single increment point covers scan and
    # reconcile; a watched fn that RAISES is a failed call, not an
    # abandonment, and leaves the streak untouched.
    # SRR-2: also record the abandoned worker itself, stamped with the
    # daemon's clock, so ``run_once`` can tell a worker that died from one
    # that is STILL ALIVE - a leaked ``_plan_lock`` only death releases.
    daemon._consecutive_abandons += 1
    daemon._abandoned_workers.append((worker, daemon._clock()))
    mark_worker_abandoned(worker)
    return False, elapsed
