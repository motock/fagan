"""Reconcile-phase watchdog tests for ``pipeline.scheduler_daemon``.

Incident 2026-09-11: reconcile iteration 44 ran 66 minutes (14:31:50Z ->
15:37:59Z) during which ``run_once`` never completed and ``last_scan_ts``
never advanced. ``scan_fn`` has been watchdog-bounded since story sh-02, but
``reconcile_fn`` is still called synchronously with no join bound, so one
wedged reconcile freezes the entire loop.

Contract pinned here:

* ``_scan_with_watchdog`` is generalised into ``_run_with_watchdog(fn, *,
  timeout_s, label)`` with the same ``(completed, result_or_elapsed)`` tuple
  and the same exception re-raise behaviour; ``_scan_with_watchdog`` survives
  as a thin delegating wrapper with its original ``(scan_fn)`` signature.
* ``reconcile_fn`` runs through the same watchdog with its own deadline from
  ``PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS`` (default 900s; malformed and
  non-positive values degrade to the default, mirroring the scan variable).
* A wedged reconcile: ``run_once`` RETURNS within a short wall-clock bound,
  ``health()["reconcile_timed_out"]`` increments, ``_last_reconcile_timeout_ts``
  is recorded, ``last_error`` names the elapsed seconds, the stall is logged
  at ERROR, the reconcile counter/timestamp still update in the ``finally``,
  and the NEXT ``run_once`` still runs the scan phase (one wedged reconcile
  costs one iteration, not the loop).
* A reconcile that RAISES is still swallowed exactly as today: ``last_error``
  is set, ``reconcile_count`` advances, ``reconcile_timed_out`` stays 0.
* ``health()`` always exposes ``reconcile_timed_out`` (0 on a clean tick) —
  the pre-existing exact-equality key pins in test_scheduler_daemon.py and
  test_scheduler_config_fingerprint.py were reconciled to admit exactly this
  key — while the scan watchdog fields stay additive-only as before.
* The KNOWN LIMITATION docstring about an abandoned worker leaking the plan
  ``_plan_lock`` must survive the generalisation untouched.

These tests are RED against the pre-watchdog implementation by design: the
bounded helper below converts a would-be suite hang into a loud assertion
failure naming the missing reconcile watchdog.
"""
import ast
import inspect
import logging
import pathlib
import re
import threading
import time
from dataclasses import dataclass

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

SCAN_ENV = "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS"
RECONCILE_ENV = "PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS"
# Generous wall-clock bound for run_once() once the (sub-second) deadline fires.
RUN_ONCE_BOUND_S = 6.0
# A wedged worker is always released eventually so leaked threads can never
# outlive the test process even if an assertion fires mid-test.
WEDGED_MAX_BLOCK_S = 30.0


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


class WedgeOnce:
    """Callable that blocks on its first invocation until ``release`` is set.

    The block is capped at ``WEDGED_MAX_BLOCK_S`` so an abandoned worker can
    never outlive the test process even if an assertion fires mid-test.
    """

    def __init__(self, release: threading.Event) -> None:
        self.calls = 0
        self._release = release

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            self._release.wait(WEDGED_MAX_BLOCK_S)


@dataclass
class DaemonFixture:
    daemon: mod.SchedulerDaemon
    clock: FakeClock
    reconcile_fn: object
    scan_fn: object
    bus: InProcessEventBus


def make_daemon(
    interval_s=60, start=0.0, reconcile_fn=None, scan_fn=None, health_path=None
) -> DaemonFixture:
    clock = FakeClock(start)
    reconcile = reconcile_fn if reconcile_fn is not None else Counter()
    scan = scan_fn if scan_fn is not None else Counter()
    bus = InProcessEventBus()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile,
        scan_fn=scan,
        bus=bus,
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=clock,
        health_path=health_path,
    )
    return DaemonFixture(daemon, clock, reconcile, scan, bus)


def run_once_bounded(daemon, bound=RUN_ONCE_BOUND_S):
    """Run ``daemon.run_once()`` on a helper thread with a hard wall-clock cap.

    Returns ``(result, elapsed_s)``. If ``run_once`` is still running after
    ``bound`` seconds, fails with an assertion that names the missing
    reconcile watchdog instead of hanging the suite. Exceptions raised by
    ``run_once`` itself are re-raised on the caller thread.
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
            f"reconcile_fn froze the whole iteration. The reconcile-phase "
            f"watchdog (worker daemon-thread + bounded join via {RECONCILE_ENV})"
            f" is missing."
        )
    if "error" in holder:
        raise holder["error"]
    return holder["result"], elapsed


def _module_source() -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Positive: a prompt reconcile (case 1)
# ---------------------------------------------------------------------------

def test_prompt_reconcile_advances_count_and_leaves_timeout_at_zero():
    f = make_daemon(interval_s=0)
    result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": True}
    health = f.daemon.health()
    assert health["reconcile_count"] == 1
    assert health["last_reconcile_ts"] is not None
    assert health.get("reconcile_timed_out") == 0, (
        "health() must always expose reconcile_timed_out (0 on a clean tick); "
        "the pre-existing exact-equality key pins were reconciled to admit it"
    )
    assert health["last_error"] is None


# ---------------------------------------------------------------------------
# Positive: a wedged reconcile is abandoned (cases 2 and 3)
# ---------------------------------------------------------------------------

def test_wedged_reconcile_is_abandoned_and_run_once_returns(monkeypatch, caplog):
    monkeypatch.delenv(SCAN_ENV, raising=False)
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    release = threading.Event()
    reconcile = WedgeOnce(release)
    f = make_daemon(interval_s=0, reconcile_fn=reconcile)
    try:
        with caplog.at_level(logging.ERROR):
            result, elapsed = run_once_bounded(f.daemon)
        assert elapsed < RUN_ONCE_BOUND_S
        assert result["scanned"] is True
        assert result["reconciled"] is False, (
            "a timed-out reconcile must not be reported as reconciled, "
            "mirroring the scan path's scanned=False on timeout"
        )
        health = f.daemon.health()
        assert health["reconcile_timed_out"] == 1
        assert getattr(f.daemon, "_reconcile_timed_out", None) == 1, (
            "the pinned counter attribute _reconcile_timed_out must increment"
        )
        assert getattr(f.daemon, "_last_reconcile_timeout_ts", None) is not None, (
            "the pinned timestamp attribute _last_reconcile_timeout_ts must be "
            "recorded"
        )
        last_error = health["last_error"]
        assert isinstance(last_error, str) and last_error
        assert re.search(r"\d", last_error), (
            f"last_error must name the elapsed seconds; got {last_error!r}"
        )
        # The reconcile counter and timestamp must still update in the
        # finally, exactly as today.
        assert health["reconcile_count"] == 1
        assert health["last_reconcile_ts"] is not None
        # The stall must be logged at ERROR, mirroring the scan path.
        assert any(
            r.levelno == logging.ERROR and "reconcile" in r.getMessage().lower()
            for r in caplog.records
        ), "a wedged reconcile must be logged at ERROR naming reconcile_fn"
    finally:
        release.set()


def test_next_run_once_still_scans_after_abandoned_reconcile(monkeypatch):
    monkeypatch.delenv(SCAN_ENV, raising=False)
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    release = threading.Event()
    reconcile = WedgeOnce(release)
    f = make_daemon(interval_s=0, reconcile_fn=reconcile)
    try:
        first, _ = run_once_bounded(f.daemon)
        assert first["reconciled"] is False
        # Let the abandoned worker die before the next tick so it cannot
        # interfere with (or outlive) the rest of the test.
        release.set()
        second, _ = run_once_bounded(f.daemon)
        assert second["scanned"] is True, (
            "one wedged reconcile costs one iteration, not the loop: the next "
            "run_once must still run the scan phase"
        )
        assert second["reconciled"] is True
        assert f.reconcile_fn.calls == 2
        health = f.daemon.health()
        assert health["reconcile_timed_out"] == 1, "the timeout must not compound"
        assert health["reconcile_count"] == 2
        assert health["scan_count"] == 2
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Negative: a raising reconcile is swallowed, not counted as a timeout (case 4)
# ---------------------------------------------------------------------------

def test_reconcile_raise_is_swallowed_not_counted_as_timeout():
    f = make_daemon(interval_s=0)
    f.reconcile_fn.raise_on_call = True
    result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": False}
    health = f.daemon.health()
    assert health["reconcile_count"] == 1
    assert health["last_reconcile_ts"] is not None
    assert isinstance(health["last_error"], str) and "boom" in health["last_error"]
    assert health.get("reconcile_timed_out") == 0, "a raise is not a timeout"
    assert getattr(f.daemon, "_reconcile_timed_out", None) == 0
    assert not getattr(f.daemon, "_last_reconcile_timeout_ts", None), (
        "a raising reconcile must not record a timeout timestamp"
    )


# ---------------------------------------------------------------------------
# Join-deadline configuration (case 5)
# ---------------------------------------------------------------------------

def test_reconcile_join_timeout_env_malformed_degrades_to_default(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "abc")
    # Must not raise, mirroring _scan_join_timeout_seconds exactly.
    assert mod._reconcile_join_timeout_seconds() == 900.0


def test_reconcile_join_timeout_default_when_unset(monkeypatch):
    monkeypatch.delenv(RECONCILE_ENV, raising=False)
    assert mod._reconcile_join_timeout_seconds() == 900.0


def test_reconcile_join_timeout_env_non_positive_degrades_to_default(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "-3")
    assert mod._reconcile_join_timeout_seconds() == 900.0


def test_reconcile_join_timeout_env_honored_when_valid(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "0.25")
    assert mod._reconcile_join_timeout_seconds() == 0.25


# ---------------------------------------------------------------------------
# The scan path is unchanged (case 6)
# ---------------------------------------------------------------------------

def test_scan_watchdog_behaviour_unchanged_wedged_scan_still_abandoned(monkeypatch):
    monkeypatch.setenv(SCAN_ENV, "0.3")
    monkeypatch.delenv(RECONCILE_ENV, raising=False)
    release = threading.Event()
    scan = WedgeOnce(release)
    f = make_daemon(interval_s=60, scan_fn=scan)
    try:
        result, _elapsed = run_once_bounded(f.daemon)
        assert result == {"scanned": False, "reconciled": False}
        health = f.daemon.health()
        assert health["scan_count"] == 1
        assert health["last_scan_ts"] is not None
        counter = getattr(f.daemon, "_scan_timed_out", None)
        if counter is None:
            counter = health.get("scan_timed_out")
        assert counter == 1, "the scan timeout counter must still increment"
        assert isinstance(health["last_error"], str)
        assert re.search(r"\d", health["last_error"]), (
            "the scan timeout message must still name the elapsed seconds"
        )
    finally:
        release.set()


def test_scan_path_unchanged_clean_tick_has_no_timeout_keys(monkeypatch):
    monkeypatch.delenv(SCAN_ENV, raising=False)
    monkeypatch.delenv(RECONCILE_ENV, raising=False)
    f = make_daemon(interval_s=60)
    result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": False}
    health = f.daemon.health()
    assert "scan_timed_out" not in health, (
        "the scan watchdog fields must stay additive-only on a clean tick"
    )
    assert health.get("reconcile_timed_out") == 0
    assert health["last_error"] is None


# ---------------------------------------------------------------------------
# The delegation shape (case 7)
# ---------------------------------------------------------------------------

def test_scan_with_watchdog_still_exists_with_original_signature():
    sig = inspect.signature(mod.SchedulerDaemon._scan_with_watchdog)
    assert list(sig.parameters) == ["self", "scan_fn"], (
        "_scan_with_watchdog must keep its original (self, scan_fn) signature"
    )
    # Behavioural: still callable exactly as before for a prompt scan.
    f = make_daemon(interval_s=60)
    completed, outcome = f.daemon._scan_with_watchdog(lambda: "scan-result")
    assert completed is True
    assert outcome == "scan-result"


def test_run_with_watchdog_exists_with_pinned_signature():
    fn = getattr(mod.SchedulerDaemon, "_run_with_watchdog", None)
    assert fn is not None, (
        "_run_with_watchdog(fn, *, timeout_s, label) is missing: "
        "_scan_with_watchdog must be generalised into a reusable helper"
    )
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["self", "fn", "timeout_s", "label"]
    for name in ("timeout_s", "label"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be keyword-only on _run_with_watchdog"
        )


def test_run_with_watchdog_returns_result_for_prompt_fn():
    f = make_daemon(interval_s=60)
    completed, outcome = f.daemon._run_with_watchdog(
        lambda: 42, timeout_s=5.0, label="probe"
    )
    assert (completed, outcome) == (True, 42)


def test_run_with_watchdog_reraises_worker_exception():
    f = make_daemon(interval_s=60)

    def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        f.daemon._run_with_watchdog(boom, timeout_s=5.0, label="probe")


def test_run_with_watchdog_abandons_wedged_fn_and_returns_elapsed():
    release = threading.Event()

    def wedged():
        release.wait(WEDGED_MAX_BLOCK_S)

    f = make_daemon(interval_s=60)
    try:
        started = time.monotonic()
        completed, outcome = f.daemon._run_with_watchdog(
            wedged, timeout_s=0.3, label="probe"
        )
        call_elapsed = time.monotonic() - started
        assert completed is False
        assert isinstance(outcome, float) and outcome > 0, (
            f"the timeout outcome must be the elapsed seconds; got {outcome!r}"
        )
        assert call_elapsed < 3.0, (
            "the caller must return promptly once the join deadline fires"
        )
    finally:
        release.set()


def test_scan_with_watchdog_delegates_to_run_with_watchdog():
    source = _module_source()
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_scan_with_watchdog"
    )
    segment = ast.get_source_segment(source, fn)
    assert segment, "could not extract the _scan_with_watchdog source"
    assert "_run_with_watchdog" in segment, (
        "_scan_with_watchdog must be a thin delegating wrapper around "
        "_run_with_watchdog (the rename-and-delegate shape used elsewhere in "
        "this codebase)"
    )
    assert "threading.Thread" not in segment, (
        "the wrapper must not inline its own worker-thread implementation "
        "anymore; the thread/join mechanics belong to _run_with_watchdog"
    )


def test_known_limitation_docstring_preserved():
    source = _module_source()
    assert "KNOWN LIMITATION (do not fix here)" in source, (
        "the KNOWN LIMITATION docstring about abandoning the worker must "
        "survive the generalisation untouched"
    )
    assert "_plan_lock" in source, (
        "the known limitation must still document that abandoning the worker "
        "leaves the plan _plan_lock held until the process dies"
    )