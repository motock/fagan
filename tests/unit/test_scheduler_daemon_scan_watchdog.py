"""Scan-phase watchdog tests for ``pipeline.scheduler_daemon`` (story sh-02).

Incident 2026-09-02: one wedged LLM call inside a scan tick froze the entire
scheduler loop for ~40 minutes because ``run_once()`` called ``scan_fn``
synchronously on the main thread while holding the plan's ``_plan_lock``.
Transport timeouts are story sh-01; this story bounds the blast radius at the
daemon level so ANY future hang degrades to one skipped tick.

Contract pinned here:

* ``scan_fn`` runs in a worker daemon-thread per ``run_once``; the parent waits
  with a bounded join controlled by ``PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS``
  (default 900 seconds).
* A wedged scan: ``run_once`` RETURNS within a generous wall-clock bound, logs
  the stall at ERROR naming the elapsed seconds, records it in
  ``health()["last_error"]`` plus an additive timeout counter/timestamp, and
  the NEXT ``run_once`` still attempts a scan (the loop keeps going).
* Under-deadline behavior is byte-for-byte identical to the pre-watchdog code:
  exact ``{"scanned": ..., "reconciled": ...}`` return shape, counts
  incremented, health file written.
* Preserved survivors: scan_fn/reconcile_fn exception swallowing, the
  reconcile-interval split, and run_forever's SIGTERM/SIGINT stop_event
  handlers.
* Each tick spawns exactly one worker; abandoned workers are daemon threads
  (they never block interpreter exit) and die once their blocked call returns.

These tests are RED against the pre-watchdog implementation by design: the
bounded helper below converts a would-be suite hang into a loud assertion
failure naming the missing watchdog.
"""
import ast
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

ENV_VAR = "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS"
# Generous wall-clock bound for run_once() once the (short) join deadline fires.
RUN_ONCE_BOUND_S = 6.0
# A wedged worker is always released eventually so leaked threads can never
# outlive the test process even if an assertion fires mid-test.
WEDGED_SCAN_MAX_BLOCK_S = 30.0


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/unit/test_scheduler_daemon.py's seams)
# ---------------------------------------------------------------------------

class FakeClock:
    """A controllable monotonic clock for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


class Counter:
    """A callable that records call count and can be made to raise."""

    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls = 0
        self.raise_on_call = raise_on_call

    def __call__(self):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("boom")


class WedgedScan:
    """A scan_fn that blocks on a threading.Event past any join deadline."""

    def __init__(self, release: threading.Event, wedge_on=None) -> None:
        self.calls = 0
        self.release = release
        # None -> wedge every call; a set -> wedge only those 1-based calls.
        self.wedge_on = wedge_on

    def __call__(self) -> None:
        self.calls += 1
        if self.wedge_on is None or self.calls in self.wedge_on:
            self.release.wait(WEDGED_SCAN_MAX_BLOCK_S)


@dataclass
class DaemonFixture:
    daemon: mod.SchedulerDaemon
    clock: FakeClock
    reconcile_fn: Counter
    scan_fn: object


def make_daemon(interval_s=60, start=0.0, scan_fn=None, health_path=None):
    clock = FakeClock(start)
    reconcile_fn = Counter()
    if scan_fn is None:
        scan_fn = Counter()
    bus = InProcessEventBus()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=clock,
        health_path=health_path,
    )
    return DaemonFixture(daemon, clock, reconcile_fn, scan_fn)


def run_once_bounded(daemon, bound=RUN_ONCE_BOUND_S):
    """Run ``daemon.run_once()`` on a helper thread with a hard wall-clock cap.

    Returns ``(result, elapsed_s, helper_thread)``. If ``run_once`` is still
    running after ``bound`` seconds, fails with an assertion that names the
    missing watchdog instead of hanging the suite. Exceptions raised by
    ``run_once`` itself are re-raised on the caller thread with their
    original traceback.
    """
    holder = {}

    def _target():
        try:
            holder["result"] = daemon.run_once()
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            holder["error"] = exc

    helper = threading.Thread(target=_target, daemon=True, name="run-once-helper")
    started = time.monotonic()
    helper.start()
    helper.join(bound)
    elapsed = time.monotonic() - started
    if helper.is_alive():
        raise AssertionError(
            f"run_once() did not return within {bound:g}s wall clock: a wedged "
            f"scan_fn froze the whole iteration. The scan-phase watchdog "
            f"(worker daemon-thread + bounded join via {ENV_VAR}) is missing."
        )
    if "error" in holder:
        raise holder["error"]
    return holder["result"], elapsed, helper


def _eventually(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


_TIMEOUT_COUNT_NAMES = (
    "scan_timed_out",
    "_scan_timed_out",
    "scan_timeout_count",
    "_scan_timeout_count",
    "scan_timeouts",
    "_scan_timeouts",
)


def timeout_counter(daemon):
    """Locate the additive scan-timeout count however the impl exposes it.

    The pre-existing test ``test_health_has_expected_keys`` pins the exact
    ``health()`` key set, so the additive counter must live on the daemon
    instance (or be added to ``health()`` only conditionally); both surfaces
    are accepted here.
    """
    for name in _TIMEOUT_COUNT_NAMES:
        value = getattr(daemon, name, None)
        if isinstance(value, int):
            return value
    health = daemon.health()
    for name in _TIMEOUT_COUNT_NAMES:
        if isinstance(health.get(name), int):
            return health[name]
    return None


def timeout_timestamp(daemon):
    """Locate the additive last-scan-timeout timestamp, if the impl has one."""
    for name in ("last_scan_timeout_ts", "_last_scan_timeout_ts"):
        if hasattr(daemon, name):
            return getattr(daemon, name)
    return daemon.health().get("last_scan_timeout_ts")


def _module_source() -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Headline: a wedged scan must cost one tick, not the whole loop
# ---------------------------------------------------------------------------

def test_run_once_returns_promptly_when_scan_blocks_past_join_deadline(
    monkeypatch,
):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(scan_fn=scan)
    try:
        result, elapsed, _helper = run_once_bounded(f.daemon)
        assert elapsed < 5.0, (
            f"run_once took {elapsed:.2f}s with a 0.5s join deadline; the "
            "watchdog must abandon the wedged scan instead of waiting for it"
        )
        # The scan did not complete, so the tick reports it as not scanned.
        assert result["scanned"] is False
        assert result["reconciled"] is False
        # Legacy keys survive; additive keys are allowed on a timeout tick.
        assert {"scanned", "reconciled"} <= set(result)
    finally:
        release.set()


def test_timeout_tick_logs_error_naming_stall_and_elapsed_seconds(
    caplog, monkeypatch
):
    monkeypatch.setenv(ENV_VAR, "1")
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(scan_fn=scan)
    try:
        with caplog.at_level(logging.ERROR):
            _result, _elapsed, _helper = run_once_bounded(f.daemon)
    finally:
        release.set()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a scan stall past the join deadline must be logged at ERROR"
    message = " ".join(r.getMessage() for r in errors)
    assert "scan" in message.lower(), (
        f"stall log must name the scan stall; got {message!r}"
    )
    numbers = [float(m) for m in re.findall(r"\d+(?:\.\d+)?", message)]
    assert any(0.9 <= n <= 5.0 for n in numbers), (
        f"stall log must name the elapsed seconds; got {message!r}"
    )


def test_timeout_tick_records_stall_in_health_and_additive_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release)
    health_path = tmp_path / "health.json"
    f = make_daemon(scan_fn=scan, health_path=str(health_path))
    try:
        result, _elapsed, _helper = run_once_bounded(f.daemon)
        assert result["scanned"] is False
        health = f.daemon.health()
        # The stall is recorded in last_error (the brief's self._last_error).
        assert isinstance(health["last_error"], str) and health["last_error"]
        assert "scan" in health["last_error"].lower()
        # The scan phase was attempted: attempt-counting semantics preserved.
        assert health["scan_count"] == 1
        assert health["last_scan_ts"] is not None
        # Additive timeout state: a count (and timestamp when present).
        count = timeout_counter(f.daemon)
        assert isinstance(count, int) and count >= 1, (
            "a timeout tick must record an additive scan_timed_out-style count"
        )
        ts = timeout_timestamp(f.daemon)
        if ts is not None:
            assert ts, "last_scan_timeout_ts must be set once a tick timed out"
        # Health is still written on a degraded tick (existing behavior).
        data = json.loads(health_path.read_text(encoding="utf-8"))
        assert data["last_error"]
        assert data["scan_count"] == 1
    finally:
        release.set()


def test_loop_keeps_going_next_run_once_still_scans_after_timeout(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release, wedge_on={1})
    f = make_daemon(scan_fn=scan)
    try:
        first, _elapsed, _helper = run_once_bounded(f.daemon)
        assert first["scanned"] is False
        assert timeout_counter(f.daemon) >= 1

        # The next tick must still attempt the scan (and succeed now).
        second = f.daemon.run_once()
        assert second == {"scanned": True, "reconciled": False}
        assert scan.calls == 2

        # The stall breadcrumb persists and the timeout count is not reset.
        health = f.daemon.health()
        assert health["last_error"] is not None
        assert "scan" in health["last_error"].lower()
        assert timeout_counter(f.daemon) >= 1
    finally:
        release.set()


def test_reconcile_still_fires_on_a_timeout_tick_when_due(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(interval_s=60, scan_fn=scan)
    try:
        f.clock.advance(60)
        result, _elapsed, _helper = run_once_bounded(f.daemon)
        # A degraded scan tick must not skip the reconcile-interval logic.
        assert result["scanned"] is False
        assert result["reconciled"] is True
        assert f.reconcile_fn.calls == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Under-deadline behavior must be byte-for-byte identical to today
# ---------------------------------------------------------------------------

def test_under_deadline_scan_is_byte_for_byte_identical(tmp_path):
    health_path = tmp_path / "health.json"
    f = make_daemon(scan_fn=Counter(), health_path=str(health_path))
    result = f.daemon.run_once()
    # Exact shape: no additive keys on a clean tick (existing tests pin this).
    assert result == {"scanned": True, "reconciled": False}
    health = f.daemon.health()
    assert health["scan_count"] == 1
    assert health["last_scan_ts"] is not None
    assert health["last_error"] is None
    assert health_path.exists(), "health file must still be written per tick"
    assert timeout_counter(f.daemon) in (0, None)
    ts = timeout_timestamp(f.daemon)
    assert not ts, "no timeout timestamp may be recorded for a clean tick"


def test_under_deadline_tick_reconciles_when_due():
    f = make_daemon(interval_s=60, scan_fn=Counter())
    f.clock.advance(60)
    result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": True}
    assert f.reconcile_fn.calls == 1


def test_scan_fn_exception_still_swallowed_and_logged(caplog):
    # Pre-watchdog behavior pinned by tests/unit/test_scheduler_daemon.py;
    # duplicated here so the watchdog refactor cannot regress it.
    f = make_daemon(interval_s=60)
    f.scan_fn.raise_on_call = True
    with caplog.at_level(logging.ERROR):
        result = f.daemon.run_once()
    assert result == {"scanned": False, "reconciled": False}
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    health = f.daemon.health()
    assert health["scan_count"] == 1
    assert isinstance(health["last_error"], str) and "boom" in health["last_error"]


# ---------------------------------------------------------------------------
# Join-deadline configuration
# ---------------------------------------------------------------------------

def test_join_deadline_env_honored_larger_deadline_lets_scan_finish(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "5")

    def slow_scan():
        time.sleep(0.3)

    f = make_daemon(scan_fn=slow_scan)
    result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": False}
    assert timeout_counter(f.daemon) in (0, None)


def test_default_join_deadline_does_not_abandon_prompt_scans(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)

    def slow_scan():
        time.sleep(0.4)

    f = make_daemon(scan_fn=slow_scan)
    result = f.daemon.run_once()
    assert result["scanned"] is True
    assert timeout_counter(f.daemon) in (0, None)


def test_default_deadline_does_not_fire_within_probe_window(monkeypatch):
    # The default is 900s: a wedged scan must NOT be abandoned within a short
    # probe window when the env var is unset. (Waiting out the full 900s is
    # not unit-testable; the 900 default itself is pinned in the source test.)
    monkeypatch.delenv(ENV_VAR, raising=False)
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(scan_fn=scan)
    holder = {}

    def _target():
        holder["result"] = f.daemon.run_once()

    helper = threading.Thread(target=_target, daemon=True)
    helper.start()
    try:
        helper.join(1.5)
        assert helper.is_alive(), (
            "the default join deadline fired within a 1.5s probe window; the "
            "default must be ~900s, not sub-second"
        )
        assert timeout_counter(f.daemon) in (0, None)
    finally:
        release.set()
        helper.join(RUN_ONCE_BOUND_S)
    assert not helper.is_alive()
    assert holder["result"]["scanned"] is True


@pytest.mark.parametrize("bad", ["not-a-number", "", "-3"])
def test_malformed_join_timeout_env_degrades_instead_of_crashing(
    monkeypatch, bad
):
    monkeypatch.setenv(ENV_VAR, bad)
    f = make_daemon(scan_fn=Counter())
    result, _elapsed, _helper = run_once_bounded(f.daemon)
    assert isinstance(result, dict)
    assert "scanned" in result


@pytest.mark.parametrize("bad", ["inf", "nan"])
def test_non_finite_join_timeout_env_degrades_to_default(monkeypatch, caplog, bad):
    """``inf`` would disable the watchdog (join never fires) and ``nan``
    gives Thread.join unspecified behaviour; both must degrade to 900.0
    with a warning, exactly like the other malformed values."""
    monkeypatch.setenv(ENV_VAR, bad)
    with caplog.at_level(logging.WARNING):
        value = mod._scan_join_timeout_seconds()
    assert value == 900.0
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"non-finite {bad!r} must be warned about, not silently accepted"
    )


# ---------------------------------------------------------------------------
# Worker-thread hygiene: exactly one per tick, daemon-flagged, reaped
# ---------------------------------------------------------------------------

def test_each_timeout_tick_spawns_exactly_one_daemon_worker(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(scan_fn=scan)
    worker = None
    try:
        baseline = set(threading.enumerate())
        result, _elapsed, helper = run_once_bounded(f.daemon)
        assert result["scanned"] is False
        new_threads = [
            t for t in threading.enumerate()
            if t not in baseline and t is not helper
        ]
        assert len(new_threads) == 1, (
            f"each tick must spawn exactly one scan worker thread; saw "
            f"{len(new_threads)}: {new_threads}"
        )
        worker = new_threads[0]
        assert worker.is_alive(), (
            "run_once must abandon (not join-through) the wedged worker"
        )
        assert worker.daemon is True, (
            "the abandoned worker must be a daemon thread so it can never "
            "block interpreter exit"
        )
        assert scan.calls == 1
    finally:
        release.set()
    if worker is not None:
        assert _eventually(lambda: not worker.is_alive()), (
            "an abandoned worker must die once its blocked call returns"
        )


def test_repeated_timeout_ticks_spawn_one_worker_each_and_all_die(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    scan = WedgedScan(release)
    f = make_daemon(scan_fn=scan)
    new_threads = []
    try:
        baseline = set(threading.enumerate())
        _r1, _e1, helper1 = run_once_bounded(f.daemon)
        _r2, _e2, helper2 = run_once_bounded(f.daemon)
        helpers = {helper1, helper2}
        new_threads = [
            t for t in threading.enumerate()
            if t not in baseline and t not in helpers
        ]
        assert len(new_threads) == 2, (
            "repeated timeout ticks must leave exactly one live worker each "
            f"(2 total), not an unbounded pile; saw {len(new_threads)}"
        )
        assert all(t.is_alive() for t in new_threads)
        assert timeout_counter(f.daemon) == 2
    finally:
        release.set()
    assert _eventually(
        lambda: not any(t.is_alive() for t in new_threads)
    ), "abandoned workers must die once their blocked calls return"


# ---------------------------------------------------------------------------
# run_forever integration: the loop survives a wedged tick
# ---------------------------------------------------------------------------

def test_run_forever_keeps_ticking_after_a_timeout_tick(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0.5")
    release = threading.Event()
    registered = []
    monkeypatch.setattr(
        mod, "signal", lambda sig, handler: registered.append(sig)
    )
    stop_event = threading.Event()
    scan = WedgedScan(release, wedge_on={1})

    def scanning():
        scan()
        if scan.calls >= 2:
            stop_event.set()

    f = make_daemon(scan_fn=scanning)
    holder = {}

    def _drive():
        try:
            f.daemon.run_forever(stop_event)
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            holder["error"] = exc

    helper = threading.Thread(target=_drive, daemon=True)
    helper.start()
    try:
        helper.join(RUN_ONCE_BOUND_S)
        assert not helper.is_alive(), (
            "run_forever must keep ticking past a timed-out scan tick"
        )
        assert "error" not in holder, (
            f"run_forever raised: {holder.get('error')!r}"
        )
    finally:
        release.set()
        helper.join(5.0)
    assert scan.calls == 2
    assert f.reconcile_fn.calls == 1  # start()'s initial sweep only
    assert timeout_counter(f.daemon) == 1
    # Survivor: SIGTERM/SIGINT handlers that set stop_event are still wired.
    assert set(registered) == {signal.SIGTERM, signal.SIGINT}


def test_run_forever_signal_survivors_unchanged():
    source = _module_source()
    assert "SIGTERM" in source and "SIGINT" in source
    assert "stop_event.set()" in source


# ---------------------------------------------------------------------------
# Interpreter exit must never be blocked by an abandoned worker
# ---------------------------------------------------------------------------

def test_abandoned_worker_never_blocks_interpreter_exit():
    script = textwrap.dedent(
        """
        import threading
        from pipeline import scheduler_daemon as mod
        from pipeline.events import InProcessEventBus

        release = threading.Event()

        def wedged():
            release.wait(30)

        daemon = mod.SchedulerDaemon(
            reconcile_fn=lambda: None,
            scan_fn=wedged,
            bus=InProcessEventBus(),
            interval_s=60,
            sleep_fn=lambda _s: None,
        )
        out = daemon.run_once()
        assert out["scanned"] is False, out
        print("WATCHDOG_OK")
        """
    )
    repo_root = pathlib.Path(mod.__file__).resolve().parents[1]
    env = dict(os.environ)
    env[ENV_VAR] = "0.5"
    env["PYTHONPATH"] = str(repo_root)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "the scheduler daemon process did not exit after abandoning a "
            "wedged scan worker; the worker must be a daemon thread so "
            "interpreter exit is never blocked"
        ) from exc
    assert "WATCHDOG_OK" in proc.stdout, (
        f"daemon subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Source-level constraints from the brief
# ---------------------------------------------------------------------------

def test_source_pins_env_var_default_and_known_limitation_comment():
    source = _module_source()
    assert ENV_VAR in source, (
        "the join deadline must be controlled by PIPELINE_SCAN_JOIN_TIMEOUT_"
        "SECONDS"
    )
    assert re.search(r"\b900\b", source), (
        "the default join deadline must be 900 seconds"
    )
    assert "_plan_lock" in source, (
        "the known limitation must be documented in a code comment: "
        "abandoning the wedged worker leaves the plan's _plan_lock held "
        "until the process dies (lock recovery is future work)"
    )


def test_scheduler_daemon_imports_remain_stdlib_or_pipeline():
    tree = ast.parse(_module_source())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        # Relative imports (level > 0) are pipeline-internal.
    allowed = set(sys.stdlib_module_names) | {"pipeline"}
    offenders = sorted(roots - allowed)
    assert not offenders, (
        f"non-stdlib imports added to pipeline/scheduler_daemon.py: "
        f"{offenders}"
    )