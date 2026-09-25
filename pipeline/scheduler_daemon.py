"""In-process scheduler daemon that owns the pipeline's cadence.

Historically an external clock (a launchd plist) fired ``advance_all_plans``
on a fixed interval. ``SchedulerDaemon`` takes ownership of that cadence so
the pipeline no longer depends on an external scheduler.

Any use of ``advance_all_plans`` for a ``__main__`` entrypoint must import it
lazily, inside a function: tests patch ``pipeline.server.advance_all_plans``,
and a module-level binding would not see the patch.
"""
import datetime as _dt
import fcntl
import json
import logging
import os
import signal as _signal_module
import subprocess
import sys
import threading
import time

from pipeline.event_wiring import build_bus
from pipeline.events import EventBus
from pipeline.paths import PLAN_DIR
from pipeline.watchers import scan_all_plans

logger = logging.getLogger(__name__)

def _checkout_git_state() -> dict:
    """Report the revision of the checkout THIS process imported ``pipeline``
    from, and how far that checkout trails its configured upstream.

    A merged change is invisible to a running scheduler until its checkout is
    pulled AND the process is restarted: the daemon imports ``pipeline.*`` from
    the checkout it was started in, so the running code is that working tree's
    revision, never the remote's.  Both values are recorded so a stale
    scheduler is distinguishable from a current one.

    ``REPO_ROOT`` is deliberately not consulted: it is a configured value (the
    installed scheduler sets it to a per-plan sentinel), not necessarily the
    tree this code was imported from.  The code's own location is the only
    reliable answer, so it is what is used.

    Never raises: missing git, a checkout that is not a repository, an
    unconfigured upstream, or a timeout leaves the value ``None``.
    """
    from pipeline import paths

    checkout = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
    state: dict = {"sha": None, "behind_origin": None}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout, check=False, capture_output=True, text=True, timeout=10,
        )
        if head.returncode == 0:
            state["sha"] = head.stdout.strip() or None
        behind = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{upstream}"],
            cwd=checkout, check=False, capture_output=True, text=True, timeout=10,
        )
        if behind.returncode == 0:
            state["behind_origin"] = int(behind.stdout.strip() or "0")
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        # Missing git, a checkout that is not a repository, an unconfigured
        # upstream, a timeout, or unparsable output: all of them leave the
        # corresponding field None rather than taking the daemon's health
        # surface (or the preflight check reading it) down. The error class is
        # logged (never the values) so a permanently blind probe is visible.
        logger.debug("checkout revision probe failed (%s)", type(exc).__name__)
    return state


# Exposed as a bare callable (rather than the module) so tests can patch it
# in isolation with a fake handler-recorder without losing access to the
# SIGTERM/SIGINT constants, which are read from ``_signal_module`` directly.
signal = _signal_module.signal

# Re-exported: callers and tests reach these names through this module.
from pipeline.scheduler_timeouts import (  # noqa: F401
    _ABANDON_RESTART_THRESHOLD_ENV,
    _ABANDON_WORKER_GRACE_ENV,
    _DEFAULT_ABANDON_RESTART_THRESHOLD,
    _DEFAULT_ABANDON_WORKER_GRACE_S,
    _DEFAULT_RECONCILE_JOIN_TIMEOUT_S,
    _DEFAULT_SCAN_JOIN_TIMEOUT_S,
    _DRAIN_JOIN_TIMEOUT_ENV,
    _DRAIN_JOIN_TIMEOUT_SECONDS,
    _RECONCILE_JOIN_TIMEOUT_ENV,
    _SCAN_JOIN_TIMEOUT_ENV,
    _abandon_restart_threshold,
    _abandon_worker_grace_seconds,
    _apply_scheduler_role_call_clamp,
    _drain_join_timeout_seconds,
    _reconcile_join_timeout_seconds,
    _scan_join_timeout_seconds,
)


class SchedulerDaemon:
    """Drives the event-driven pipeline's scan + reconcile cadence.

    Collaborators are dependency-injected so the daemon is fully testable
    without real time, real processes, or real plans.
    """

    def __init__(
        self,
        reconcile_fn,
        scan_fn,
        bus: EventBus,
        interval_s: float = 60,
        sleep_fn=time.sleep,
        clock=time.monotonic,
        health_path=None,
    ) -> None:
        self._reconcile_fn = reconcile_fn
        self._scan_fn = scan_fn
        self._bus = bus
        self._interval_s = interval_s
        self._sleep_fn = sleep_fn
        self._clock = clock
        # Interval gating: last time we reconciled.
        self._last_reconcile = clock()
        # Health surface state
        self._health_path = health_path
        self._last_reconcile_ts = None
        self._last_scan_ts = None
        self._last_error = None
        self._reconcile_count = 0
        self._scan_count = 0
        # Additive scan-watchdog state (story sh-02). Kept off the ``health()``
        # dict until a timeout actually happens because existing tests pin the
        # exact clean-tick key set.
        self._scan_timed_out = 0
        self._last_scan_timeout_ts = None
        # Reconcile-phase watchdog state (story LOCKSTARVE-C1). Unlike the
        # scan fields above, ``reconcile_timed_out`` is always present in
        # ``health()`` (0 on a clean tick) — the pinned exact-equality key
        # tests were reconciled to admit it.
        self._reconcile_timed_out = 0
        self._last_reconcile_timeout_ts = None
        # Per-attempt flag backing health()'s additive
        # ``last_reconcile_timeout_ts``: True only while the MOST RECENT
        # reconcile attempt timed out. Reset at the start of every attempt so
        # a later clean tick's health() returns exactly the seven pinned keys
        # again (no stale timestamp leaking in) while the cumulative
        # ``reconcile_timed_out`` counter keeps reflecting the earlier
        # timeout.
        self._last_reconcile_attempt_timed_out = False
        # Abandon-restart escape hatch state (story LOCKSTARVE-C2): the
        # CONSECUTIVE watchdog-worker abandonment streak. Incremented only in
        # ``_run_with_watchdog``'s abandonment branch (one point covers both
        # the scan and reconcile phases) and reset to 0 by ``run_once`` on
        # any tick whose phases all completed normally — consecutive, never
        # cumulative, so abandonments spread across a healthy day never
        # trigger a restart. Surfaced in ``health()`` only while non-zero
        # (the gated ``scan_timed_out`` pattern) so a clean tick's key set
        # stays byte-for-byte unchanged.
        self._consecutive_abandons = 0
        # Abandoned-worker ledger (story SRR-2): every watchdog worker that
        # was abandoned, as ``(thread, monotonic timestamp)`` pairs stamped at
        # the abandonment. A worker still alive long after its abandonment is
        # a blocked call that has not returned — the plan ``_plan_lock`` it
        # holds is leaked on a horizon the daemon cannot influence, and
        # process death is the only release. ``run_once`` prunes dead workers
        # each tick and raises SystemExit(1) once a survivor outlives the
        # grace window (``_abandon_worker_grace_seconds``).
        self._abandoned_workers = []
        # The most recent drain worker (story SWH-03): kept across ticks so a
        # worker abandoned by the join timeout below can be recognized as
        # still alive on the next tick, which must skip its drain rather than
        # start a second drainer that would race the outbox merge and drop a
        # spooled notification. Overwritten only when a new worker is started.
        self._drain_worker = None

    def start(self) -> None:
        """Perform an immediate reconcile sweep on startup.

        This method is idempotent for the caller: it calls ``_reconcile_fn``
        exactly once, updates health metrics and interval gating state.
        It does not swallow exceptions; any error propagates to the caller.
        """
        # Perform reconcile immediately.
        self._reconcile_fn()
        now_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self._last_reconcile_ts = now_ts
        self._reconcile_count += 1
        # Update interval gating clock so run_once does not reconcile again
        # until the configured interval has elapsed.
        self._last_reconcile = self._clock()

    def health(self) -> dict:
        """Return a snapshot of the daemon's health state."""
        snapshot = {
            "alive": True,
            "last_reconcile_ts": self._last_reconcile_ts,
            "last_scan_ts": self._last_scan_ts,
            "last_error": self._last_error,
            "reconcile_count": self._reconcile_count,
            "scan_count": self._scan_count,
            "reconcile_timed_out": self._reconcile_timed_out,
        }
        # The watchdog timestamp fields stay additive-only: each appears only
        # on a tick whose phase actually timed out, so the clean-tick
        # health() key set stays exactly the seven pinned keys
        # (reconcile_timed_out is always present; the scan counter is
        # surfaced additively alongside its timestamp, mirroring it).
        if self._scan_timed_out:
            snapshot["scan_timed_out"] = self._scan_timed_out
            snapshot["last_scan_timeout_ts"] = self._last_scan_timeout_ts
        if self._last_reconcile_attempt_timed_out:
            snapshot["last_reconcile_timeout_ts"] = (
                self._last_reconcile_timeout_ts
            )
        if self._consecutive_abandons:
            # Abandon-restart streak (story LOCKSTARVE-C2), gated exactly
            # like ``scan_timed_out``: present only once it is non-zero so
            # the clean-tick key set stays byte-for-byte unchanged.
            snapshot["consecutive_abandonments"] = self._consecutive_abandons
        abandoned_workers_alive = sum(
            1 for worker, _ts in self._abandoned_workers if worker.is_alive()
        )
        if abandoned_workers_alive:
            # Abandoned-worker leak detector (story SRR-2), gated exactly
            # like ``scan_timed_out``: present only while the count is
            # non-zero so the clean-tick key set stays byte-for-byte
            # unchanged. Read-only: health() never prunes the ledger, so a
            # health poll cannot change the streak gate on the next tick.
            snapshot["abandoned_workers_alive"] = abandoned_workers_alive
        return snapshot

    def config_fingerprint(self) -> dict:
        """Return the config THIS process actually resolved (story CFG-B1).

        Additive surface only: this deliberately lives OUTSIDE ``health()``
        because existing tests pin health()'s exact key set. Every value is
        read at call time (module-attribute access, nothing cached on self)
        so a patched ``pipeline.paths.PLAN_DIR`` is visible immediately and
        the fingerprint always describes the current process.

        ``dispatch_backend`` is ``None`` when ``PIPELINE_BACKEND_DISPATCH``
        is unset or empty — never a ``KeyError`` and never an empty string.
        """
        # Imported lazily (not at module scope) so the values are read live
        # on every call; ``from pipeline.paths import PLAN_DIR`` at the top
        # of this module is an import-time snapshot and must not be used.
        from pipeline import config, paths

        checkout = _checkout_git_state()
        return {
            "plan_dir": str(paths.PLAN_DIR),
            "worktree_root": str(paths.WORKTREE_ROOT),
            "autonomy": config.PIPELINE_AUTONOMY,
            "dispatch_backend": os.environ.get("PIPELINE_BACKEND_DISPATCH")
            or None,
            "checkout_sha": checkout["sha"],
            "checkout_behind_origin": checkout["behind_origin"],
            "pid": os.getpid(),
        }

    def write_health(self, path: str) -> None:
        """Atomically write the health JSON to *path*.

        The file is written to ``<path>.tmp`` first and then moved into place
        with :func:`os.replace` for atomicity.

        The file payload is ``{**health(), "config": config_fingerprint()}``:
        the seven pinned health keys (the six originally pinned keys plus the
        always-present ``reconcile_timed_out`` counter) plus the config
        fingerprint this process resolved, so the dashboard's
        ``/api/health`` (and the preflight divergence check) can compare it
        against their own resolution. ``health()`` itself returns exactly
        that pinned seven-key set on a clean tick — the ``config`` key
        exists only in the file. On a timeout tick the watchdog fields
        (``scan_timed_out``/``last_scan_timeout_ts`` when the scan timed
        out, ``last_reconcile_timeout_ts`` when the reconcile timed out)
        are additive extras in both the dict and the file.
        """
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({**self.health(), "config": self.config_fingerprint()}, fh)
        os.replace(tmp_path, path)

    def _run_with_watchdog(self, fn, *, timeout_s, label):
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
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised by caller
                box["error"] = exc

        worker = threading.Thread(
            target=_worker, name=f"scheduler-{label}-worker", daemon=True
        )
        worker.start()
        started = time.monotonic()
        # Thread.join(timeout) ALWAYS returns None; the is_alive() check below
        # is the only correct timed-out-vs-done signal.
        worker.join(timeout_s)
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
        # daemon's monotonic clock, so ``run_once`` can tell a worker that
        # died (its blocked call returned) from one that is STILL ALIVE — a
        # leaked plan ``_plan_lock`` that only process death releases.
        self._consecutive_abandons += 1
        self._abandoned_workers.append((worker, self._clock()))
        return False, elapsed

    def _scan_with_watchdog(self, scan_fn):
        """Run ``scan_fn`` in one worker daemon-thread with a bounded join.

        Thin delegating wrapper around :meth:`_run_with_watchdog` (the
        rename-and-delegate shape used elsewhere in this codebase, e.g.
        ``_advance_pipeline_locked`` / ``_advance_pipeline_locked_impl``),
        kept so existing callers and tests are untouched.

        KNOWN LIMITATION (inherited from ``_run_with_watchdog``): abandoning
        the worker leaves any plan ``_plan_lock`` held by the wedged call
        locked until the process dies. Escape hatch (story LOCKSTARVE-C2):
        after ``PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD`` (default 3)
        CONSECUTIVE abandonments the daemon writes health, logs an ERROR, and
        raises ``SystemExit(1)`` so launchd (KeepAlive=true) restarts the
        process — process death is the only thing that releases the leaked
        flock; values <= 0 disable the hatch.
        """
        return self._run_with_watchdog(
            scan_fn, timeout_s=_scan_join_timeout_seconds(), label="scan"
        )

    def run_once(self) -> dict:
        """Perform one iteration: scan first, then reconcile if due.

        ``scan_fn`` is the cheap, event-driven path and runs every call.
        ``reconcile_fn`` (the watchdog/recovery sweep) only runs once
        ``interval_s`` has elapsed on the injected clock since the last
        reconcile. Exceptions from either are logged and swallowed so a single
        bad plan cannot kill the loop.

        If the consecutive watchdog-abandonment streak has reached
        ``PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD`` (story LOCKSTARVE-C2),
        the health file is written first and THEN ``SystemExit(1)`` is
        raised, so launchd restarts the process and releases any leaked plan
        flock.
        """
        scanned = False
        reconciled = False
        # Streak snapshot for this tick (story LOCKSTARVE-C2): if no phase
        # abandons a worker below, every phase completed normally and the
        # streak ends — reset it after the phases.
        streak_at_start = self._consecutive_abandons

        # Scan phase – always attempted, count regardless of success. The scan
        # runs in a worker thread bounded by a join deadline (story sh-02) so
        # a wedged scan costs one tick instead of the whole loop.
        try:
            completed, outcome = self._scan_with_watchdog(self._scan_fn)
            if completed:
                scanned = True
            else:
                elapsed_s = outcome
                logger.error(
                    "scan_fn stalled past the %.1fs join deadline "
                    "(PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS); abandoning the "
                    "worker after %.1fs and continuing the loop",
                    _scan_join_timeout_seconds(),
                    elapsed_s,
                )
                self._scan_timed_out += 1
                self._last_scan_timeout_ts = time.time()
                self._last_error = (
                    f"scan_fn stalled past the join deadline "
                    f"({elapsed_s:.1f}s elapsed); worker abandoned"
                )
        except Exception as exc:  # pragma: no cover - exercised via tests
            logger.exception("scan_fn raised during scheduler iteration")
            self._last_error = str(exc)
        finally:
            # Update health metrics for scan.
            self._scan_count += 1
            self._last_scan_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            # Write health at the END of the scan phase rather than only at
            # the end of the tick: the scan is the cheap, event-driven path,
            # and the phases after it (reconcile, drain) can each hold a tick
            # open for minutes. Refreshing here means a daemon that is stuck
            # in a later phase still shows a live scan_count instead of a
            # timestamp frozen at the previous complete tick.
            if self._health_path is not None:
                try:
                    self.write_health(self._health_path)
                except Exception:  # pragma: no cover - must never kill the loop
                    logger.exception("write_health failed after scan phase")

        now = self._clock()
        # Boundary tolerance: ``now - self._last_reconcile`` is a difference
        # of two large monotonic readings, and float rounding of
        # ``(t0 + interval) - t0`` can land epsilon BELOW ``interval`` even
        # when a full interval has elapsed (catastrophic cancellation on a
        # large t0 — observed on CI: a 60s advance measured as
        # 59.99999999999909, which silently skipped the reconcile phase). A
        # full-interval advance must never be skipped to rounding, so
        # compare against ``interval_s - epsilon``: the epsilon is ~1e-9,
        # far below any real interval, so a genuinely-shorter elapsed (59s
        # against a 60s interval) still skips and only the exact-boundary
        # rounding artefact is absorbed.
        if now - self._last_reconcile >= self._interval_s - max(
            1e-9, abs(self._interval_s) * 1e-12
        ):
            # Capture the deadline once: the timeout branch below must log
            # the deadline that was actually enforced, not re-read mutable
            # env state a second time.
            timeout_s = _reconcile_join_timeout_seconds()
            # This attempt starts clean: the additive
            # last_reconcile_timeout_ts must reflect only the most recent
            # attempt, never an earlier tick's timeout.
            self._last_reconcile_attempt_timed_out = False
            try:
                completed, outcome = self._run_with_watchdog(
                    self._reconcile_fn,
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
                    self._reconcile_timed_out += 1
                    self._last_reconcile_timeout_ts = time.time()
                    self._last_reconcile_attempt_timed_out = True
                    self._last_error = (
                        f"reconcile_fn stalled past the join deadline "
                        f"({elapsed_s:.1f}s elapsed); worker abandoned"
                    )
            except Exception as exc:  # pragma: no cover - exercised via tests
                logger.exception("reconcile_fn raised during scheduler iteration")
                self._last_error = str(exc)
            finally:
                # Update health metrics for reconcile regardless of success.
                self._reconcile_count += 1
                self._last_reconcile_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                self._last_reconcile = now

        # Drain phase (PLANNOTIFY-06): its own phase after scan and reconcile,
        # in its own try/except so a drain failure can never kill the loop or
        # perturb the scanned/reconciled accounting above. The imports are
        # function-local so importing this module does not pull in smtplib.
        # ALL_PLANS drains every outbox file in PLAN_DIR in one call, including
        # a plan whose manifest was since removed — a per-manifest glob would
        # silently orphan that plan's queued notifications forever.
        try:
            from pipeline.notification_email import send_notification_email
            from pipeline.notification_outbox import ALL_PLANS, drain_outbox

            def _drain_outbox_worker() -> None:
                # Caught here rather than in the enclosing ``except`` below:
                # this body runs on ``drain_worker``, so a raise would escape
                # to that thread's excepthook instead of reaching the
                # caller's handler. Same log-and-swallow contract as before
                # the drain became bounded.
                try:
                    drain_outbox(ALL_PLANS, send_notification_email)
                except Exception:
                    logger.exception(
                        "notification outbox drain failed during tick"
                    )

            if self._drain_worker is not None and self._drain_worker.is_alive():
                # A previous tick's drain worker was abandoned by the join
                # timeout below and is STILL running (a wedged SMTP send,
                # say). Starting a second drainer now would race it:
                # ``_drain_one_outbox``'s merge assumes its snapshot is a
                # prefix of the current file, so a drainer that rewrites the
                # file shorter while another is mid-drain makes the second
                # one silently drop a record the sink spooled in between.
                # Skip this tick's drain instead; the still-running worker
                # (or the next tick, once it has exited) picks the queue up.
                logger.warning(
                    "previous notification outbox drain worker is still "
                    "alive; skipping this tick's drain to avoid two "
                    "concurrent drainers racing the outbox merge"
                )
            else:
                drain_worker = threading.Thread(
                    target=_drain_outbox_worker,
                    name="scheduler-drain-worker",
                    daemon=True,
                )
                # Remember the worker BEFORE joining it: the join below may
                # abandon it while it is still alive, and the next tick needs
                # this reference to skip rather than overlap the drain.
                self._drain_worker = drain_worker
                drain_worker.start()
                drain_worker.join(_drain_join_timeout_seconds())
                if drain_worker.is_alive():
                    # A wedged drain (an unreachable SMTP host, say) must not hold
                    # the tick open: the liveness and streak checks below only run
                    # once this phase returns, so an unbounded drain makes a stuck
                    # daemon look alive. Deliberately NOT routed through
                    # _run_with_watchdog: a slow drain is not evidence of a leaked
                    # plan lock, and feeding it into _consecutive_abandons would
                    # let an SMTP outage trip the abandon-restart SystemExit and
                    # bounce the scheduler.
                    logger.warning(
                        "notification outbox drain exceeded %.1fs; abandoning the "
                        "worker and continuing the tick",
                        _drain_join_timeout_seconds(),
                    )
        except Exception:
            logger.exception("notification outbox drain failed during tick")

        # SRR-2: the abandoned-worker ledger is the streak's missing half.
        # Prune workers whose blocked call has returned (dead = the lock it
        # held was released by normal completion), then treat a survivor that
        # has outlived the grace window as a leaked plan ``_plan_lock``: the
        # daemon cannot influence its release, so process death must. This
        # runs BEFORE the streak-reset/threshold block so the grace exit wins
        # over the threshold exit and the reset below sees the pruned ledger.
        self._abandoned_workers = [
            (worker, ts)
            for worker, ts in self._abandoned_workers
            if worker.is_alive()
        ]
        grace_s = _abandon_worker_grace_seconds()
        now = self._clock()
        # Boundary tolerance: ``now - ts`` is a difference of two large
        # monotonic readings, and float rounding of ``(t0 + grace) - t0`` can
        # land epsilon ABOVE ``grace`` even when the elapsed time is exactly
        # the grace window (catastrophic cancellation on a large t0 — observed
        # on CI: elapsed 0.20000000018626451 against a 0.2s grace). A worker
        # exactly at the grace is NOT past it, so compare against
        # ``grace_s + epsilon``: the epsilon is ~1e-9, nine orders of
        # magnitude below the 300s default, so a genuinely-stuck worker
        # (elapsed 301s against a 300s grace) still exits and only the
        # exact-boundary rounding artefact is absorbed.
        grace_boundary = grace_s + max(1e-9, abs(grace_s) * 1e-12)
        leaked = [
            (worker, now - ts)
            for worker, ts in self._abandoned_workers
            if now - ts > grace_boundary
        ]
        if leaked:
            names = ", ".join(
                f"{worker.name!r} (alive {elapsed:.0f}s past its abandonment)"
                for worker, elapsed in leaked
            )
            self._last_error = (
                f"abandoned watchdog worker(s) {names} still alive past the "
                f"{grace_s:.0f}s grace window: the plan _plan_lock held by "
                "the wedged call is leaked and process death is the only "
                "thing that releases the flock — exiting so launchd "
                "restarts the daemon"
            )
            logger.error("%s", self._last_error)
            # Same pre-exit ordering as the abandon-restart hatch below:
            # write the health file FIRST so the final state (including the
            # ``abandoned_workers_alive`` count) is observable on disk, THEN
            # raise SystemExit.
            if self._health_path is not None:
                try:
                    self.write_health(self._health_path)
                except Exception:  # pragma: no cover - unlikely but safe
                    logger.exception("write_health failed")
            raise SystemExit(1)
        for worker, ts in self._abandoned_workers:
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

        # LOCKSTARVE-C2: a tick in which no phase abandoned a worker means
        # every phase completed normally, so the consecutive-abandonment
        # streak ends. The reset happens BEFORE the threshold check (and
        # before the health write, so the file reflects the reset) — the
        # threshold is re-read from the env per call, and checking it first
        # could exit on a healthy tick after an operator lowered the
        # threshold under an existing streak.
        # SRR-2: the reset's premise — "every phase completed normally" — is
        # FALSE while an abandoned worker is still alive: its leaked plan
        # ``_plan_lock`` persists and the hatch must stay armed, so the reset
        # fires only when the pruned ledger is empty. With an empty ledger
        # this is byte-identical to the pre-SRR-2 behaviour.
        if not self._abandoned_workers and (
            self._consecutive_abandons == streak_at_start
        ):
            self._consecutive_abandons = 0

        if self._health_path is not None:
            try:
                self.write_health(self._health_path)
            except Exception:  # pragma: no cover - unlikely but safe
                logger.exception("write_health failed")

        # Abandon-restart escape hatch (story LOCKSTARVE-C2): process death
        # is the only thing that releases a flock, and the launchd job has
        # KeepAlive=true, so exiting IS the recovery. This runs AFTER the
        # health write so the final state is observable on disk, and it
        # raises SystemExit (never the hard process-kill, which would skip
        # the health write). SystemExit inherits BaseException, so the
        # ``except Exception`` handlers above let it propagate. ``>=`` rather
        # than ``==``: a single tick can abandon more than one worker's worth
        # of streak (e.g. an operator lowering the threshold mid-streak), and
        # that must still restart.
        threshold = _abandon_restart_threshold()
        if threshold > 0 and self._consecutive_abandons >= threshold:
            logger.error(
                "%d consecutive watchdog worker abandonment(s) reached the "
                "abandon-restart threshold of %d (%s); a leaked plan "
                "_plan_lock held by an abandoned worker is the suspected "
                "cause, and process death is the only thing that releases "
                "the flock — exiting so launchd restarts the daemon",
                self._consecutive_abandons,
                threshold,
                _ABANDON_RESTART_THRESHOLD_ENV,
            )
            raise SystemExit(1)

        return {"scanned": scanned, "reconciled": reconciled}

    def run_forever(self, stop_event) -> None:
        """Run until ``stop_event`` is set.

        Registers SIGTERM/SIGINT handlers that set ``stop_event`` so the daemon
        shuts down gracefully. The handlers never call :func:`sys.exit`.
        """

        def _handler(signum, frame):  # pragma: no cover - exercised via tests
            stop_event.set()

        signal(_signal_module.SIGTERM, _handler)
        signal(_signal_module.SIGINT, _handler)

        # Perform an initial reconcile sweep before entering the loop.
        self.start()

        while True:
            self.run_once()
            self._sleep_fn(1)
            if stop_event.is_set():
                break


def run_daemon() -> int:
    """Production composition root: build real collaborators and run.

    Acquires an exclusive, non-blocking single-instance lock at
    ``PLAN_DIR / ".scheduler_daemon.lock"`` before touching anything else, so
    two daemons can never run two clocks. If another instance already holds
    the lock, this returns 1 immediately without calling ``scan_fn``,
    ``reconcile_fn``, or ``run_forever``.
    """
    raw_interval = os.environ.get("PIPELINE_SCHEDULER_INTERVAL_S", "60")
    try:
        interval_s = int(raw_interval)
    except ValueError:
        print(
            f"scheduler_daemon: PIPELINE_SCHEDULER_INTERVAL_S must be an "
            f"integer, got {raw_interval!r}",
            file=sys.stderr,
        )
        return 1
    if interval_s <= 0:
        print(
            f"scheduler_daemon: PIPELINE_SCHEDULER_INTERVAL_S must be a "
            f"positive integer, got {interval_s}",
            file=sys.stderr,
        )
        return 1

    lock_path = PLAN_DIR / ".scheduler_daemon.lock"
    # Held for the process lifetime: do not close this fd or let it go out of
    # scope before run_forever() runs, or the flock releases early.
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            "scheduler_daemon: another instance already holds the lock at "
            f"{lock_path}, exiting",
            file=sys.stderr,
        )
        os.close(lock_fd)
        return 1

    health_path = os.environ.get("PIPELINE_SCHEDULER_HEALTH_PATH") or str(
        PLAN_DIR / ".scheduler_health.json"
    )

    bus = build_bus()

    def scan_fn():
        scan_all_plans(bus)

    # Clamp the scheduler process's per-call model budget BEFORE the lazy
    # import below: once advance_all_plans is in scope the first tick's
    # stacked model calls can run, and the clamp must already be in effect
    # (fail-closed semantics unchanged - a clamped-out call raises exactly
    # the RuntimeError complete() raises today).
    _apply_scheduler_role_call_clamp()

    # Resolved at call time (not at module load) so tests patching
    # ``pipeline.server.advance_all_plans`` land on this call site.
    from .server import advance_all_plans

    daemon = SchedulerDaemon(
        reconcile_fn=advance_all_plans,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=interval_s,
        health_path=health_path,
    )
    daemon.run_forever(threading.Event())
    return 0


if __name__ == "__main__":
    sys.exit(run_daemon())
