"""Abandoned-worker grace / leaked-lock detection tests (story SRR-2).

Incident 2026-09-14: after an abandonment leaked the plan ``_plan_lock``,
every subsequent tick SKIPPED the locked plan quickly and completed, so the
LOCKSTARVE-C2 streak reset on every tick and the escape hatch never fired:
``reconcile_timed_out`` climbed, ``health()["alive"]`` stayed true, and the
plan stayed locked forever.

The daemon knows something the streak cannot express: whether a
previously-abandoned worker thread is STILL ALIVE. A worker still alive long
after its abandonment is a blocked call that has not returned; the flock it
holds is leaked on a horizon the daemon cannot influence, and process death
is the only release.

Contract pinned here:

* ``_run_with_watchdog`` records every abandoned worker as
  ``(thread, monotonic_timestamp)`` in ``self._abandoned_workers``
  (initialised to ``[]`` in ``__init__``), right where
  ``_consecutive_abandons`` is incremented.
* ``_abandon_worker_grace_seconds()`` reads
  ``PIPELINE_ABANDON_WORKER_GRACE_SECONDS`` per call, default 300.0;
  malformed / non-finite / non-positive values degrade to the default with a
  warning naming the env var (mirroring ``_reconcile_join_timeout_seconds``).
* In ``run_once``, AFTER the phases and BEFORE the streak-reset/threshold
  block: dead workers are pruned from the ledger; an abandoned worker still
  alive with ``now - ts > grace`` is a leaked lock -> ``_last_error`` names
  the abandoned worker and the leaked plan lock, health is written, and THEN
  ``SystemExit(1)`` is raised. Alive but still under grace -> a WARNING
  naming the wait, no exit.
* The streak reset does NOT fire while any abandoned worker is still alive
  ("every phase completed normally" is false while the leaked lock persists);
  when the pruned ledger is empty the reset is byte-identical to today.
* ``health()`` exposes ``abandoned_workers_alive`` (count) ONLY when nonzero,
  so a clean tick's key set stays byte-identical.

RED by design until the SRR-2 production edit lands: the tests below fail on a
missing ``_abandoned_workers`` ledger, a missing
``_abandon_worker_grace_seconds`` resolver, a never-raised grace
``SystemExit``, an unexposed ``abandoned_workers_alive`` field, and a streak
reset that still fires while a leaked lock persists.

Clock discipline: the injected :class:`FakeClock` starts at the real
``time.monotonic()`` value so these tests are correct whether the
implementation stamps/compares the abandonment with the injected clock or
with ``time.monotonic()`` directly. ``advance_past_grace`` moves BOTH the
injected clock and real time past the grace, so no implementation choice can
make the grace window untestable.
"""
import json
import logging
import pathlib
import re
import threading
import time

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

GRACE_ENV = "PIPELINE_ABANDON_WORKER_GRACE_SECONDS"
DEFAULT_GRACE_S = 300.0
THRESHOLD_ENV = "PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"

# The join deadline the fakes run against: short enough that an abandoned
# worker costs ~0.05s, long enough to be deterministic on a loaded CI box.
JOIN_TIMEOUT_S = 0.05
# A wedged worker is always released eventually so leaked threads can never
# outlive the test process even if an assertion fires mid-test.
RELEASE_BOUND_S = 30.0
# Generous wall-clock bound for run_once() once the (short) join deadline
# fires; converts a would-be suite hang into a loud failure.
RUN_ONCE_BOUND_S = 6.0
# Small grace so a test can cross it without waiting 300s of wall clock.
TEST_GRACE_S = 0.2
# A threshold high enough that the LOCKSTARVE-C2 hatch can never fire, so an
# exit observed in these tests can only come from the new grace path.
HATCH_OUT_OF_THE_WAY = "100"

# The seven pinned clean-tick health keys (fixed anchor). Membership only —
# never an exact total key set, so a later sibling adding another field
# cannot break these tests.
PINNED_CLEAN_KEYS = frozenset(
    {
        "alive",
        "last_reconcile_ts",
        "last_scan_ts",
        "last_error",
        "reconcile_count",
        "scan_count",
        "reconcile_timed_out",
    }
)

# Words a warning about a still-alive abandoned worker is expected to use
# when it "names the wait".
_WAIT_WORDS = ("grace", "wait", "alive", "still", "second")


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/unit/test_scheduler_daemon_abandon_restart.py's seams)
# ---------------------------------------------------------------------------

class FakeClock:
    """A controllable monotonic clock for tests.

    ``start`` defaults to the real ``time.monotonic()`` value so the injected
    clock and the real clock agree at t0: the grace window is then testable
    whichever clock the implementation stamps the abandonment with.
    """

    def __init__(self, start=None) -> None:
        self._now = time.monotonic() if start is None else start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


class Counter:
    """A collaborator that records its call count and returns immediately."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class WedgedCall:
    """A collaborator that blocks past any join deadline on selected calls.

    ``wedge_on=None`` wedges every call; a set wedges only those 1-based
    calls. Blocked calls are released via the shared ``release`` event (set in
    each test's ``finally``) so abandoned workers always die.
    """

    def __init__(self, release: threading.Event, wedge_on=None) -> None:
        self.calls = 0
        self.release = release
        self.wedge_on = wedge_on

    def __call__(self) -> None:
        self.calls += 1
        if self.wedge_on is None or self.calls in self.wedge_on:
            self.release.wait(RELEASE_BOUND_S)


def make_daemon(
    interval_s=60,
    start=None,
    scan_fn=None,
    reconcile_fn=None,
    health_path=None,
):
    clock = FakeClock(start)
    if scan_fn is None:
        scan_fn = Counter()
    if reconcile_fn is None:
        reconcile_fn = Counter()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=InProcessEventBus(),
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=clock,
        health_path=health_path,
    )
    return daemon, clock


def _run_on_thread(target, bound, what):
    """Run ``target()`` on a helper thread with a hard wall-clock cap.

    Exceptions (including SystemExit, which is a BaseException) are re-raised
    on the caller thread with their original traceback.
    """
    holder = {}

    def _target():
        try:
            holder["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            holder["error"] = exc

    helper = threading.Thread(
        target=_target, daemon=True, name="abandon-grace-helper"
    )
    helper.start()
    helper.join(bound)
    if helper.is_alive():
        raise AssertionError(
            f"{what} did not return within {bound:g}s wall clock; the "
            f"abandoned-worker grace path must never hang the loop"
        )
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def run_once_bounded(daemon, bound=RUN_ONCE_BOUND_S):
    return _run_on_thread(daemon.run_once, bound, "run_once()")


def run_once_expect_normal(daemon):
    """``run_once`` must return normally — a premature SystemExit fails loud."""
    try:
        return run_once_bounded(daemon)
    except SystemExit as exc:
        pytest.fail(
            f"run_once raised SystemExit({exc.code!r}) when no abandoned "
            f"worker had outlived the grace window: the SRR-2 grace exit "
            f"fired prematurely"
        )


def patch_join_timeouts(monkeypatch):
    """Make both watchdog phases abandon after JOIN_TIMEOUT_S instead of 900s."""
    monkeypatch.setattr(
        mod, "_scan_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setattr(
        mod, "_reconcile_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )


def _eventually(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def abandoned_workers(daemon):
    """The daemon's abandoned-worker ledger, however it is exposed."""
    ledger = getattr(daemon, "_abandoned_workers", None)
    assert ledger is not None, (
        "SchedulerDaemon must track abandoned workers in "
        "self._abandoned_workers (a list of (thread, monotonic timestamp) "
        "entries) so a still-alive worker can be told apart from a dead one"
    )
    return ledger


def alive_worker_count(daemon):
    return sum(1 for worker, _ts in abandoned_workers(daemon) if worker.is_alive())


def advance_past_grace(clock, grace=TEST_GRACE_S):
    """Move BOTH the injected clock and real time past ``grace``.

    The injected clock is advanced so an implementation that stamps/compares
    with ``self._clock()`` crosses the grace; the real sleep covers an
    implementation that uses ``time.monotonic()`` for both ends.
    """
    clock.advance(grace * 2)
    time.sleep(grace * 2)


def _module_source() -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) The abandoned-worker ledger
# ---------------------------------------------------------------------------

def test_abandoned_workers_ledger_initialised_empty():
    """``__init__`` must initialise ``_abandoned_workers`` alongside the streak."""
    daemon, _clock = make_daemon()
    assert abandoned_workers(daemon) == [], (
        "a fresh daemon must start with an empty abandoned-worker ledger"
    )


def test_abandoned_worker_is_recorded_with_monotonic_timestamp(monkeypatch):
    """``_run_with_watchdog`` appends ``(thread, monotonic ts)`` on abandonment."""
    patch_join_timeouts(monkeypatch)
    release = threading.Event()
    scan = WedgedCall(release)
    daemon, clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        ledger = abandoned_workers(daemon)
        assert len(ledger) == 1, (
            f"abandoning one worker must record exactly one ledger entry; "
            f"saw {len(ledger)}"
        )
        worker, ts = ledger[0]
        assert worker.is_alive(), (
            "the recorded worker is the abandoned (still-blocked) worker, so "
            "it must still be alive right after the abandonment"
        )
        assert worker.daemon is True, (
            "the abandoned worker must be a daemon thread so it can never "
            "block interpreter exit"
        )
        assert isinstance(ts, (int, float)) and not isinstance(ts, bool), (
            f"the ledger timestamp must be a monotonic number; got {ts!r}"
        )
        assert abs(ts - clock()) < 5.0, (
            "the abandonment timestamp must come from the daemon's monotonic "
            "clock so the grace window is testable with an injected clock"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (2) Past grace -> leaked lock -> health write THEN SystemExit(1)
# ---------------------------------------------------------------------------

def test_worker_alive_past_grace_exits_and_names_leaked_lock(
    monkeypatch, tmp_path, caplog
):
    """A worker still alive past the grace is a leaked lock: exit 1.

    Tick 2 abandons NOTHING (only the first scan call wedges), so this also
    pins that the grace check runs on every tick, after the phases and before
    the streak-reset/threshold block — not only on a tick that abandons.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    health_path = tmp_path / "health.json"
    daemon, clock = make_daemon(scan_fn=scan, health_path=str(health_path))
    try:
        run_once_expect_normal(daemon)  # tick 1: abandon W1 (still alive)
        assert alive_worker_count(daemon) == 1
        advance_past_grace(clock)
        with caplog.at_level(
            logging.ERROR, logger="pipeline.scheduler_daemon"
        ), pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # tick 2: W1 past grace -> leaked lock
        assert exc_info.value.code == 1, (
            f"a worker still alive past the grace window must exit 1 so "
            f"launchd restarts the daemon and releases the leaked flock; got "
            f"{exc_info.value.code!r}"
        )

        last_error = daemon.health()["last_error"]
        assert isinstance(last_error, str) and last_error, (
            f"the grace exit must record a last_error; got {last_error!r}"
        )
        lowered = last_error.lower()
        assert "abandon" in lowered, (
            f"last_error must name the abandoned worker; got {last_error!r}"
        )
        assert "lock" in lowered, (
            f"last_error must name the leaked plan lock; got {last_error!r}"
        )

        assert health_path.exists(), (
            "the health file must be written BEFORE the SystemExit is raised "
            "so the final state is observable on disk"
        )
        data = json.loads(health_path.read_text(encoding="utf-8"))
        assert data["abandoned_workers_alive"] == 1, (
            f"the on-disk health must expose the live abandoned-worker count; "
            f"got {data.get('abandoned_workers_alive')!r}"
        )
        assert "lock" in str(data["last_error"]).lower(), (
            f"the on-disk last_error must name the leaked plan lock; got "
            f"{data.get('last_error')!r}"
        )
    finally:
        release.set()


def test_worker_alive_past_grace_exit_is_logged_at_error(monkeypatch, caplog):
    """The grace exit must be loud: an ERROR naming the leaked plan lock."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        advance_past_grace(clock)
        with caplog.at_level(
            logging.ERROR, logger="pipeline.scheduler_daemon"
        ), pytest.raises(SystemExit):
            run_once_bounded(daemon)
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
        ]
        assert any("lock" in m.lower() for m in errors), (
            "the grace exit must log at ERROR naming the leaked plan lock as "
            f"the suspected cause; saw {errors}"
        )
        assert any("abandon" in m.lower() for m in errors), (
            f"the grace ERROR must name the abandoned worker; saw {errors}"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (2b) Alive but under grace -> warn, no exit, streak NOT reset
# ---------------------------------------------------------------------------

def test_worker_alive_under_grace_warns_without_exiting(monkeypatch, caplog):
    """Under grace: a WARNING naming the wait, and no SystemExit."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # tick 1: abandon W1
        with caplog.at_level(
            logging.WARNING, logger="pipeline.scheduler_daemon"
        ):
            result = run_once_expect_normal(daemon)  # tick 2: clean, W1 alive
        assert result == {"scanned": True, "reconciled": False}
        assert alive_worker_count(daemon) == 1, (
            "a worker under the grace window must NOT be pruned"
        )
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any(
            "abandon" in m.lower()
            and any(word in m.lower() for word in _WAIT_WORDS)
            for m in warnings
        ), (
            "a worker alive but still under the grace window must log a "
            f"WARNING naming the wait; saw {warnings}"
        )
    finally:
        release.set()


def test_streak_not_reset_while_abandoned_worker_still_alive(monkeypatch):
    """The reset's premise is false while the leaked lock persists.

    Tick 1 abandons W1 (streak 1). Tick 2 completes every phase normally, so
    today's code would reset the streak to 0 — but W1 is still alive, so the
    streak must survive. This is the 2026-09-14 bug: the reset fired on every
    tick and the hatch never reached its threshold.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # tick 1: abandon W1 -> streak 1
        assert daemon.health()["consecutive_abandonments"] == 1
        result = run_once_expect_normal(daemon)  # tick 2: clean, W1 alive
        assert result == {"scanned": True, "reconciled": False}
        assert alive_worker_count(daemon) == 1
        assert daemon.health()["consecutive_abandonments"] == 1, (
            "the consecutive-abandonment streak must NOT reset while an "
            "abandoned worker is still alive: 'every phase completed "
            "normally' is false while the leaked plan lock persists"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (3) Dead worker -> pruned, streak resets exactly as today
# ---------------------------------------------------------------------------

def test_dead_worker_is_pruned_and_streak_resets_as_today(monkeypatch):
    """A worker that died by the next tick keeps today's reset behaviour."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # tick 1: abandon W1 -> streak 1
        assert daemon.health()["consecutive_abandonments"] == 1
        release.set()  # let the abandoned worker's blocked call return
        assert _eventually(lambda: alive_worker_count(daemon) == 0), (
            "the abandoned worker must die once its blocked call returns"
        )
        result = run_once_expect_normal(daemon)  # tick 2: clean, W1 dead
        assert result == {"scanned": True, "reconciled": False}
        assert abandoned_workers(daemon) == [], (
            "a dead abandoned worker must be pruned from the ledger"
        )
        assert "consecutive_abandonments" not in daemon.health(), (
            "with no abandoned worker alive the streak reset must fire "
            "exactly as it does today (streak 0 -> key absent)"
        )
    finally:
        release.set()


def test_health_omits_abandoned_workers_alive_once_workers_die(monkeypatch):
    """The additive field disappears again once the ledger is pruned empty."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        assert daemon.health()["abandoned_workers_alive"] == 1
        release.set()
        assert _eventually(lambda: alive_worker_count(daemon) == 0)
        run_once_expect_normal(daemon)  # prunes the dead worker
        assert "abandoned_workers_alive" not in daemon.health(), (
            "the additive field must be present ONLY while the count is "
            "nonzero, so a clean tick's key set stays byte-identical"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (4) health() additive field
# ---------------------------------------------------------------------------

def test_health_exposes_abandoned_workers_alive_only_when_nonzero(monkeypatch):
    """``abandoned_workers_alive`` is an additive, gated watchdog field."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        clean = daemon.health()
        assert "abandoned_workers_alive" not in clean, (
            "the new watchdog field must be additive-only: absent on a clean "
            f"tick; saw {sorted(clean)}"
        )
        assert PINNED_CLEAN_KEYS <= set(clean), (
            "the pinned clean-tick health keys must all still be present; "
            f"missing {sorted(PINNED_CLEAN_KEYS - set(clean))}"
        )
        run_once_expect_normal(daemon)  # abandon W1
        after = daemon.health()
        assert after["abandoned_workers_alive"] == 1, (
            f"once an abandoned worker is alive the count must be exposed; "
            f"got {after.get('abandoned_workers_alive')!r}"
        )
    finally:
        release.set()


def test_multiple_abandoned_workers_are_counted(monkeypatch):
    """The field is a COUNT of live abandoned workers, not a boolean."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        run_once_expect_normal(daemon)
        assert alive_worker_count(daemon) == 2
        assert daemon.health()["abandoned_workers_alive"] == 2, (
            "two still-alive abandoned workers must be counted as 2"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (5) Boundary: exactly at the grace window is NOT past it
# ---------------------------------------------------------------------------

def test_worker_alive_exactly_at_grace_does_not_exit(monkeypatch):
    """``now - ts > grace`` is strict: exactly at grace must not exit."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        clock.advance(TEST_GRACE_S)  # exactly at the grace, not past it
        result = run_once_expect_normal(daemon)
        assert result == {"scanned": True, "reconciled": False}
        assert alive_worker_count(daemon) == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (6) The LOCKSTARVE-C2 threshold hatch is unchanged
# ---------------------------------------------------------------------------

def test_threshold_hatch_still_fires(monkeypatch, caplog):
    """The existing consecutive-abandonment hatch keeps working."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(THRESHOLD_ENV, "2")
    monkeypatch.setenv(GRACE_ENV, str(DEFAULT_GRACE_S))
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # streak 1
        with caplog.at_level(
            logging.ERROR, logger="pipeline.scheduler_daemon"
        ), pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # streak 2 == threshold
        assert exc_info.value.code == 1, (
            f"the LOCKSTARVE-C2 hatch must still exit 1 at the threshold; got "
            f"{exc_info.value.code!r}"
        )
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
        ]
        assert any("lock" in m.lower() for m in errors), (
            f"the threshold hatch must still log the leaked-lock cause; saw "
            f"{errors}"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (7) The grace resolver
# ---------------------------------------------------------------------------

def test_grace_resolver_default_and_read_per_call(monkeypatch):
    """Unset -> 300.0; the value is re-read on every call, never cached."""
    helper = getattr(mod, "_abandon_worker_grace_seconds", None)
    assert helper is not None, (
        "pipeline.scheduler_daemon must define "
        "_abandon_worker_grace_seconds() reading "
        f"{GRACE_ENV}"
    )
    monkeypatch.delenv(GRACE_ENV, raising=False)
    assert helper() == DEFAULT_GRACE_S, (
        f"unset {GRACE_ENV} must yield the default {DEFAULT_GRACE_S}"
    )
    monkeypatch.setenv(GRACE_ENV, "12.5")
    assert helper() == 12.5, "the grace must be read per call, never cached"
    monkeypatch.setenv(GRACE_ENV, "7")
    assert helper() == 7.0, "the grace must be re-read on every call"


@pytest.mark.parametrize(
    "bad", ["abc", "", "0", "-1", "-0.5", "inf", "-inf", "nan"]
)
def test_grace_resolver_degrades_to_default_with_warning(
    monkeypatch, caplog, bad
):
    """Malformed / non-positive / non-finite -> default 300.0 + a warning.

    Mirrors ``_reconcile_join_timeout_seconds``: a bad operator override must
    never take the daemon down, and a non-finite grace must never silently
    disable the leak detector.
    """
    helper = getattr(mod, "_abandon_worker_grace_seconds", None)
    assert helper is not None, (
        "pipeline.scheduler_daemon must define "
        "_abandon_worker_grace_seconds()"
    )
    monkeypatch.setenv(GRACE_ENV, bad)
    with caplog.at_level(logging.WARNING, logger="pipeline.scheduler_daemon"):
        value = helper()
    assert value == DEFAULT_GRACE_S, (
        f"{GRACE_ENV}={bad!r} must degrade to the default "
        f"{DEFAULT_GRACE_S}, not crash and not disable the detector; got "
        f"{value!r}"
    )
    assert any(
        r.levelno >= logging.WARNING and GRACE_ENV in r.getMessage()
        for r in caplog.records
    ), (
        f"a malformed {GRACE_ENV} must emit a warning naming the env var; "
        f"saw {[r.getMessage() for r in caplog.records]}"
    )


def test_malformed_grace_env_does_not_crash_the_loop(monkeypatch):
    """A bad grace override must never take the daemon down."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, "not-a-number")
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1})
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)
        result = run_once_expect_normal(daemon)
        assert result == {"scanned": True, "reconciled": False}
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (8) Source pins
# ---------------------------------------------------------------------------

def test_source_pins_grace_env_var_and_default():
    source = _module_source()
    assert GRACE_ENV in source, (
        f"the grace window must be controlled by {GRACE_ENV}"
    )
    assert re.search(r"\b300(?:\.0)?\b", source), (
        "the default grace window must be 300 seconds"
    )
    assert "_abandoned_workers" in source, (
        "the abandoned-worker ledger must be part of the daemon's state"
    )
    assert "_plan_lock" in source, (
        "the leaked plan lock must stay documented in the module"
    )
    assert re.search(r"grace", source, re.IGNORECASE), (
        "the module must document the grace window that separates a slow "
        "worker from a leaked lock"
    )


# ---------------------------------------------------------------------------
# (9) Ordering: the grace check precedes the streak-reset/threshold block
# ---------------------------------------------------------------------------

def test_grace_exit_precedes_the_threshold_hatch(monkeypatch):
    """The grace check runs BEFORE the streak-reset/threshold block.

    Tick 2 satisfies BOTH conditions: the streak reaches the threshold AND an
    earlier abandoned worker is past its grace. The grace path must win, so
    ``last_error`` names the leaked plan lock (the threshold path leaves the
    abandonment stall message, which never mentions a lock).
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, "2")
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # tick 1: abandon W1 -> streak 1
        advance_past_grace(clock)
        with pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # tick 2: streak 2 AND W1 past grace
        assert exc_info.value.code == 1
        last_error = daemon.health()["last_error"]
        assert isinstance(last_error, str) and "lock" in last_error.lower(), (
            "the grace check must run BEFORE the streak-reset/threshold "
            "block, so the leaked-lock message wins over the threshold "
            f"message; got {last_error!r}"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (10) Boundary: an empty ledger never exits, however far the clock jumps
# ---------------------------------------------------------------------------

def test_empty_ledger_clock_jump_does_not_exit(monkeypatch):
    """No abandoned workers -> no grace exit, no matter how far time moves."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    daemon, clock = make_daemon(scan_fn=Counter())
    clock.advance(TEST_GRACE_S * 100)
    result = run_once_expect_normal(daemon)
    assert result == {"scanned": True, "reconciled": False}
    assert "abandoned_workers_alive" not in daemon.health()


def test_ledger_is_a_list_of_pairs():
    """The ledger is a list of ``(thread, timestamp)`` pairs."""
    daemon, _clock = make_daemon()
    ledger = abandoned_workers(daemon)
    assert isinstance(ledger, list), (
        f"_abandoned_workers must be a list; got {type(ledger).__name__}"
    )


# ---------------------------------------------------------------------------
# (11) The reconcile phase's abandonment is covered too
# ---------------------------------------------------------------------------

def test_reconcile_phase_abandonment_also_triggers_grace_exit(monkeypatch):
    """The ledger is shared: a wedged reconcile leaks the lock just as badly."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(GRACE_ENV, str(TEST_GRACE_S))
    monkeypatch.setenv(THRESHOLD_ENV, HATCH_OUT_OF_THE_WAY)
    release = threading.Event()
    reconcile = WedgedCall(release, wedge_on={1})
    daemon, clock = make_daemon(
        scan_fn=Counter(), reconcile_fn=reconcile, interval_s=60
    )
    try:
        clock.advance(60)  # make reconcile due on tick 1
        run_once_expect_normal(daemon)  # tick 1: abandon the reconcile worker
        assert alive_worker_count(daemon) == 1
        advance_past_grace(clock)
        with pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # tick 2: past grace -> leaked lock
        assert exc_info.value.code == 1
        last_error = daemon.health()["last_error"]
        assert "lock" in str(last_error).lower(), (
            f"a wedged reconcile's leaked lock must be named; got {last_error!r}"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (12) DO NOT TOUCH: SRR-1 clamp, watchdog defaults, concurrency.py
# ---------------------------------------------------------------------------

def test_srr1_clamp_call_still_precedes_the_lazy_server_import():
    """SRR-1's clamp call in ``run_daemon`` must not be disturbed."""
    source = _module_source()
    assert "_apply_scheduler_role_call_clamp" in source, (
        "SRR-1's scheduler role-call clamp must survive this story"
    )
    run_daemon_at = source.index("def run_daemon")
    clamp_at = source.index("_apply_scheduler_role_call_clamp()", run_daemon_at)
    import_at = source.index("from .server import advance_all_plans", run_daemon_at)
    assert clamp_at < import_at, (
        "the SRR-1 clamp must still be applied BEFORE the lazy "
        "``from .server import advance_all_plans`` import in run_daemon()"
    )


def test_watchdog_join_timeout_defaults_unchanged():
    """The scan/reconcile join deadline defaults must stay 900s."""
    assert mod._DEFAULT_SCAN_JOIN_TIMEOUT_S == 900.0, (
        "the scan watchdog's default join deadline must stay 900s"
    )
    assert mod._DEFAULT_RECONCILE_JOIN_TIMEOUT_S == 900.0, (
        "the reconcile watchdog's default join deadline must stay 900s"
    )


def test_concurrency_module_gains_no_lock_reclamation_api():
    """In-process ``_plan_lock`` reclamation stays out of scope.

    A flock held by a blocked thread cannot be safely stolen; process death
    remains the release, and this story only makes that automatic.
    """
    from pipeline import concurrency

    assert hasattr(concurrency, "_plan_lock"), (
        "pipeline.concurrency._plan_lock must remain the plan lock primitive"
    )
    pattern = re.compile(
        r"reclaim|steal|force_release|force_unlock", re.IGNORECASE
    )
    offenders = sorted(
        name for name in dir(concurrency) if pattern.search(name)
    )
    assert not offenders, (
        "pipeline/concurrency.py must not gain an in-process lock "
        f"reclamation API; saw {offenders}"
    )
    exported = getattr(concurrency, "__all__", [])
    assert not [name for name in exported if pattern.search(name)], (
        f"no lock-reclamation name may be exported; saw {sorted(exported)}"
    )
