"""Health-signalling helpers for the scheduler daemon.

Moved out of ``pipeline/scheduler_daemon.py`` without behaviour change: the
bounded-join watchdog, the heartbeat that keeps health advancing while a phase
runs, and the once-per-revision stale-checkout notice. The ``SchedulerDaemon``
methods of the same names delegate here. Daemon state (``_heartbeat_phase``,
``_consecutive_abandons``, ...) stays on the daemon instance, so these helpers
read and write the same fields.

The logger keeps the ``pipeline.scheduler_daemon`` name: the daemon's log
channel is unchanged by where the code lives.
"""
import datetime as _dt
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from types import FrameType

from pipeline import manifest_source
from pipeline.scheduler_timeouts import _heartbeat_interval_seconds

from .actor import SYSTEM_SCHEDULER, bind_principal
from .dispatch_fence import mark_worker_abandoned

logger = logging.getLogger("pipeline.scheduler_daemon")

# Upstream revisions already announced as stale (story SSA-1). Module-level so
# the dedup survives across the daemon's many health writes; never reassigned.
STALE_CHECKOUT_NOTIFIED: set[str] = set()


# A stuck worker's stack is truncated to its last lines: the innermost frames
# (the wedged call) are the useful ones, and an unbounded stack would bloat
# the log line for a deep recursion.
_STUCK_STACK_MAX_LINES = 60


def describe_stuck_worker(worker) -> dict:
    """Return ``{"stack": ..., "plan_name": ...}`` for an abandoned *worker*.

    ``stack`` is the formatted stack of the thread's live frame (its last
    ``_STUCK_STACK_MAX_LINES`` lines), ``""`` when the thread has no live
    frame any more. ``plan_name`` is the first ``str`` ``plan_name`` local
    found walking from that frame outwards, ``""`` when there is none — the
    daemon logs it so triage knows which plan's ``_plan_lock`` is leaked.

    Only that single local is read: a wedged reconcile frame can hold a
    token, a request body or a diff, and none of it may reach a log record.
    Never raises — the caller is in the middle of abandoning a worker and
    must not turn a wedged phase into a crashed daemon.
    """
    try:
        frame = sys._current_frames().get(worker.ident)
        if frame is None:
            return {"stack": "", "plan_name": ""}
        plan_name = ""
        probe: FrameType | None = frame
        while probe is not None:
            candidate = probe.f_locals.get("plan_name")
            if isinstance(candidate, str):
                plan_name = candidate
                break
            probe = probe.f_back
        stack = "".join(traceback.format_stack(frame))
        lines = stack.splitlines()[-_STUCK_STACK_MAX_LINES:]
        return {"stack": "\n".join(lines), "plan_name": plan_name}
    except Exception:  # pragma: no cover - defensive: must never raise
        logger.debug("describe_stuck_worker failed", exc_info=True)
        return {"stack": "", "plan_name": ""}


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
        except Exception as exc:  # noqa: BLE001 - re-raised by caller
            box["error"] = exc
        except BaseException as exc:
            box["error"] = exc
            raise  # SystemExit/KeyboardInterrupt: caller re-raises it too

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
    # cops-03: the daemon can neither join nor kill this thread, so the only
    # actionable evidence of what wedged is its stack and the plan it was
    # working on. Log it exactly once, here, at the abandonment point: the
    # per-tick "still alive" warning repeats for the whole grace window and
    # says nothing about WHERE the worker is stuck.
    stuck = describe_stuck_worker(worker)
    logger.error(
        "watched phase abandoned; stuck worker stack follows",
        extra={
            "phase": label,
            "worker": worker.name,
            "elapsed_s": round(elapsed, 1),
            "plan_name": stuck["plan_name"],
            "stack": stuck["stack"],
        },
    )
    return False, elapsed


def notify_if_checkout_stale(health: dict) -> None:
    """Notify once per upstream revision when the daemon is running stale code.

    A merged orchestration change is invisible to this process until it is
    restarted. ``checkout_behind_origin`` — computed by
    ``config_fingerprint()``, deliberately NOT by ``health()``, whose key
    set is pinned — says the checkout trails upstream; this function adds the
    two facts that decide whether that matters: the upstream revision (for
    one-notice-per-revision dedup) and whether the missing commits touch
    ``pipeline/`` or ``app/``. Doc-only and test-only merges stay silent.

    *health* is the config fingerprint dict, not ``health()``.
    """
    from pipeline import paths

    behind = health.get("checkout_behind_origin")
    if not behind:
        return
    checkout = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))

    def _git(*args: str):
        return subprocess.run(
            ["git", *args], cwd=checkout, check=False,
            capture_output=True, text=True, timeout=10,
        )

    # Resolve the upstream revision first: once announced, the diff probe
    # is pure waste, and this runs on every health write.
    upstream = _git("rev-parse", "@{upstream}")
    if upstream.returncode != 0:
        return
    upstream_sha = upstream.stdout.strip()
    if not upstream_sha or upstream_sha in STALE_CHECKOUT_NOTIFIED:
        return
    diff = _git("diff", "--name-only", "HEAD..@{upstream}")
    if diff.returncode != 0:
        return
    code_paths = [
        name for name in diff.stdout.splitlines()
        if name.startswith(("pipeline/", "app/"))
    ]
    if not code_paths:
        return
    try:
        names = manifest_source.read_plan_names(fallback_on_error=True)
    except Exception:  # a notice must never break the daemon
        logger.debug("plan listing for the stale-checkout notice failed", exc_info=True)
        return
    if not names:
        return
    plan_name = names[0]
    from .persistence import _notify_user

    _notify_user(
        plan_name,
        f"The running scheduler is {behind} commit(s) behind its upstream "
        f"and the missing commits change code (e.g. {code_paths[0]}). The "
        "daemon does not hot-reload: run scripts/reload_pipeline_daemon.sh "
        "to activate the merged code.",
        event="scheduler_reload_required",
        dedup_key=f"scheduler_stale:{upstream_sha}",
    )
    STALE_CHECKOUT_NOTIFIED.add(upstream_sha)
