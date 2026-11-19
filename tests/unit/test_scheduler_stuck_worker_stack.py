"""Stuck-worker stack logging for abandoned watchdog phases.

An abandoned watchdog worker is the one piece of state the daemon cannot act
on: the thread is wedged inside a call that never returns, so the plan
``_plan_lock`` it holds is leaked and only process death releases it. Until
now the daemon said "a worker is still alive" every loop iteration (once a
second for the whole grace window) and never said WHERE the worker was
stuck, so incident triage had nothing to act on.

Contract pinned here:

* ``pipeline.scheduler_daemon_health.describe_stuck_worker(worker)`` returns
  a dict with a ``stack`` (the formatted stack of the worker's live frame)
  and a ``plan_name`` (the plan the wedged call was working on, or ``""``).
  It never raises: a worker that is gone (or a bogus argument) yields
  ``{"stack": "", "plan_name": ""}``.
* ``run_with_watchdog`` logs exactly ONE ERROR record per abandonment
  announcing that the stuck worker's stack follows, carrying structured
  extras: ``phase``, ``worker``, ``elapsed_s``, ``plan_name`` and ``stack``.
  The stack is the worker's live stack truncated to its last 60 lines.
* Only the ``plan_name`` local is ever read out of the stuck frame: no other
  local (a token, a request body, a diff, file contents) may reach a log
  record.
* The daemon's per-iteration "still alive ... waiting out the grace window"
  WARNING is rate limited to at most one emission per worker per 30 seconds;
  the first emission and the final grace-exit ERROR are kept.

RED by design until the production edit lands: the tests below fail on a
missing ``describe_stuck_worker`` helper, a missing abandonment ERROR record
and its extras, and an un-rate-limited grace-window WARNING.

Clock discipline (copied from
``tests/unit/test_scheduler_daemon_abandoned_worker_grace.py``): the injected
:class:`FakeClock` starts at the real ``time.monotonic()`` value and is
advanced explicitly by the test, so the daemon's grace window and its
warning cadence are observable without waiting in wall-clock time.
"""
import logging
import threading
import time

import pytest

from pipeline import scheduler_daemon as daemon_mod
from pipeline import scheduler_daemon_health as health_mod
from pipeline.events import InProcessEventBus

LOGGER_NAME = "pipeline.scheduler_daemon"
# The join deadline the fakes run against: short enough to abandon quickly,
# long enough to be deterministic on a loaded CI box.
JOIN_TIMEOUT_S = 0.05
# A wedged worker is always released eventually so leaked threads can never
# outlive the test process even if an assertion fires mid-test.
RELEASE_BOUND_S = 30.0
GRACE_ENV = "PIPELINE_ABANDON_WORKER_GRACE_SECONDS"
THRESHOLD_ENV = "PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"
# The secret the stuck frame holds: it must never reach a log record. It is
# assembled at runtime so the literal never appears in this file's source
# (the logged stack carries the SOURCE LINE of every frame, so a literal
# secret written here would legitimately show up in the stack).
SECRET = "s3" + "cret"
SECRET_TOKEN = SECRET
# How far the fake clock is driven for the warning-rate-limit test, in
# seconds of daemon time (~one loop iteration per second).
RATE_LIMIT_WINDOW_S = 65
# The rate limit the daemon is expected to honour, in seconds per worker.
WARNING_INTERVAL_S = 30
# The stack is truncated to its last N lines.
MAX_STACK_LINES = 60


class FakeClock:
    """A controllable monotonic clock for tests.

    ``start`` defaults to the real ``time.monotonic()`` value so the injected
    clock and the real clock agree at t0.
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


class WedgedScan:
    """A scan that wedges on selected calls, so N workers leak into the ledger.

    Later calls return immediately: the daemon keeps ticking with the
    abandoned-but-alive workers in its ledger, which is the state the
    grace-window WARNING is emitted for.
    """

    def __init__(self, release, wedge_calls=None) -> None:
        self.calls = 0
        self.release = release
        self.wedge_calls = {1} if wedge_calls is None else wedge_calls

    def __call__(self) -> None:
        self.calls += 1
        if self.calls in self.wedge_calls:
            self.release.wait(RELEASE_BOUND_S)


def make_daemon(clock=None, scan_fn=None, reconcile_fn=None):
    """Build a real ``SchedulerDaemon`` with injected seams."""
    clock = FakeClock() if clock is None else clock
    daemon = daemon_mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn if reconcile_fn is not None else Counter(),
        scan_fn=scan_fn if scan_fn is not None else Counter(),
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
        clock=clock,
        health_path=None,
    )
    return daemon, clock


def stuck_worker_helper():
    """Resolve ``describe_stuck_worker``, failing loudly when it is missing."""
    helper = getattr(health_mod, "describe_stuck_worker", None)
    if helper is None:
        helper = getattr(daemon_mod, "describe_stuck_worker", None)
    if helper is None:
        pytest.fail(
            "describe_stuck_worker(worker) is missing: it must live in "
            "pipeline.scheduler_daemon_health (next to run_with_watchdog) and "
            "return a dict with the stuck worker's 'stack' and 'plan_name'"
        )
    return helper


# ---------------------------------------------------------------------------
# Stuck frames used by the tests
# ---------------------------------------------------------------------------

def _block_with_plan_name(entered, release):
    """A wedged call whose frame carries a ``plan_name`` local.

    The secret is read from a module-level constant instead of being spelled
    out here: the stack the daemon logs contains the SOURCE LINE of every
    frame, so a literal secret written here would legitimately show up in the
    logged stack and the secrecy assertion would be untestable.
    """
    token = SECRET_TOKEN
    plan_name = "p-demo"
    entered.set()
    release.wait(RELEASE_BOUND_S)
    return plan_name, token


def _block_without_plan_name(entered, release):
    """A wedged call whose frames carry no ``plan_name`` local."""
    entered.set()
    release.wait(RELEASE_BOUND_S)


def _outer_with_plan_name(entered, release):
    """A wedged call one frame below the frame that holds the plan name."""
    plan_name = "p-outer"
    return plan_name, _block_without_plan_name(entered, release)


def _block_with_non_str_plan_name(entered, release):
    """A wedged call whose only ``plan_name`` local is not a string."""
    plan_name = 42
    entered.set()
    release.wait(RELEASE_BOUND_S)
    return plan_name


def _innermost_marker(release):
    """The deepest frame of the deep-stack test: blocks here."""
    release.wait(RELEASE_BOUND_S)


def _recurse_to_bottom(depth, release):
    if depth > 0:
        return _recurse_to_bottom(depth - 1, release)
    return _innermost_marker(release)


def _block_deep(entered, release):
    """A wedged call buried under ~120 frames of recursion."""
    entered.set()
    return _recurse_to_bottom(120, release)


@pytest.fixture
def release_event():
    """A release event every wedged thread in a test waits on."""
    event = threading.Event()
    try:
        yield event
    finally:
        event.set()


def _start_blocked(target, release):
    """Start ``target`` and wait until it is blocked inside it."""
    entered = threading.Event()
    thread = threading.Thread(
        target=target, args=(entered, release), name="stuck-worker", daemon=True
    )
    thread.start()
    assert entered.wait(5.0), "the wedged thread never reached its blocking call"
    return thread


def _error_records(caplog):
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def _grace_warning_records(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == LOGGER_NAME
        and "still alive" in record.getMessage()
        and "waiting out" in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# describe_stuck_worker
# ---------------------------------------------------------------------------

def test_describe_stuck_worker_reports_plan_name_and_stack(release_event):
    """A live wedged thread: the plan name and the stack are both reported."""
    thread = _start_blocked(_block_with_plan_name, release_event)
    result = stuck_worker_helper()(thread)

    assert isinstance(result, dict), (
        f"the helper must return a dict; got {result!r}"
    )
    assert "plan_name" in result and "stack" in result, (
        f"the helper must report both 'plan_name' and 'stack'; got {result!r}"
    )
    assert result["plan_name"] == "p-demo", (
        f"the plan name of the wedged call must be reported; got "
        f"{result['plan_name']!r}"
    )
    assert isinstance(result["stack"], str) and result["stack"], (
        f"the stack of the wedged call must be reported; got {result['stack']!r}"
    )
    assert "_block_with_plan_name" in result["stack"], (
        f"the stack must name the wedged function; got {result['stack']!r}"
    )


def test_describe_stuck_worker_without_plan_name_is_empty_and_still_has_stack(
    release_event,
):
    """No ``plan_name`` anywhere in the frames: empty plan, non-empty stack."""
    thread = _start_blocked(_block_without_plan_name, release_event)
    result = stuck_worker_helper()(thread)

    assert result["plan_name"] == "", (
        f"a frame without a string plan_name must report an empty plan; got "
        f"{result['plan_name']!r}"
    )
    assert isinstance(result["stack"], str) and result["stack"], (
        f"the stack must still be reported when no plan name is found; got "
        f"{result['stack']!r}"
    )


def test_describe_stuck_worker_ignores_non_string_plan_name(release_event):
    """A ``plan_name`` local that is not a string is not a plan name."""
    thread = _start_blocked(_block_with_non_str_plan_name, release_event)
    result = stuck_worker_helper()(thread)

    assert result["plan_name"] == "", (
        f"only a string plan_name counts as a plan; got {result['plan_name']!r}"
    )
    assert result["stack"], "the stack must still be reported"


def test_describe_stuck_worker_finds_plan_name_in_an_outer_frame(release_event):
    """The plan name is looked up by walking the stuck worker's frames."""
    thread = _start_blocked(_outer_with_plan_name, release_event)
    result = stuck_worker_helper()(thread)

    assert result["plan_name"] == "p-outer", (
        f"the plan name of the wedged call's caller must be reported; got "
        f"{result['plan_name']!r}"
    )
    assert result["stack"], "the stack must still be reported"


def test_describe_stuck_worker_for_finished_thread_is_empty():
    """A thread that already finished has no live frame: both fields empty."""
    thread = threading.Thread(target=lambda: None, name="finished-worker")
    thread.start()
    thread.join(5.0)
    assert not thread.is_alive(), "the finished thread must have exited"

    result = stuck_worker_helper()(thread)

    assert result.get("stack") == "", (
        f"a finished thread has no live stack; got {result.get('stack')!r}"
    )
    assert result.get("plan_name") == "", (
        f"a finished thread has no plan; got {result.get('plan_name')!r}"
    )


def test_describe_stuck_worker_never_raises_on_bogus_worker():
    """The helper is diagnostic code: it must never take the daemon down."""
    helper = stuck_worker_helper()
    for bogus in (None, object(), "not-a-thread"):
        result = helper(bogus)
        assert isinstance(result, dict), (
            f"the helper must return a dict; got {result!r}"
        )
        assert result.get("stack") == "", (
            f"a bogus worker has no stack; got {result.get('stack')!r}"
        )
        assert result.get("plan_name") == "", (
            f"a bogus worker has no plan; got {result.get('plan_name')!r}"
        )


def test_describe_stuck_worker_truncates_a_deep_stack(release_event):
    """A very deep stack is truncated to its last 60 lines."""
    thread = _start_blocked(_block_deep, release_event)
    result = stuck_worker_helper()(thread)

    stack = result["stack"]
    assert stack, "the stack must be reported for a deep stack"
    lines = stack.splitlines()
    assert len(lines) <= MAX_STACK_LINES, (
        f"the stack must be truncated to the last {MAX_STACK_LINES} lines; got "
        f"{len(lines)} lines"
    )
    assert "_innermost_marker" in stack, (
        "the truncated stack must still name the frame where the worker is "
        f"stuck; got {stack!r}"
    )


# ---------------------------------------------------------------------------
# run_with_watchdog: the abandonment ERROR record
# ---------------------------------------------------------------------------

def test_run_with_watchdog_logs_one_error_with_stack_extras(caplog):
    """An abandonment logs exactly one ERROR record with the structured extras."""
    daemon, _clock = make_daemon()
    release = threading.Event()
    try:

        def blocked():
            token = SECRET_TOKEN
            plan_name = "p-demo"
            release.wait(RELEASE_BOUND_S)
            return plan_name, token

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            completed, elapsed = health_mod.run_with_watchdog(
                daemon, blocked, timeout_s=JOIN_TIMEOUT_S, label="probe"
            )
        assert completed is False, "the wedged phase must not report completion"

        errors = _error_records(caplog)
        assert len(errors) == 1, (
            f"an abandonment must log exactly one ERROR record; got "
            f"{[record.getMessage() for record in errors]!r}"
        )
        record = errors[0]
        message = record.getMessage()
        assert "abandoned" in message, (
            f"the abandonment ERROR must say the phase was abandoned; got "
            f"{message!r}"
        )
        assert "stack" in message, (
            f"the abandonment ERROR must announce the stack; got {message!r}"
        )
        assert getattr(record, "phase", None) == "probe", (
            f"the ERROR must name the abandoned phase; got "
            f"{getattr(record, 'phase', None)!r}"
        )
        assert getattr(record, "plan_name", None) == "p-demo", (
            f"the ERROR must name the plan the wedged call was working on; got "
            f"{getattr(record, 'plan_name', None)!r}"
        )
        stack = getattr(record, "stack", None)
        assert isinstance(stack, str) and stack, (
            f"the ERROR must carry the stuck worker's stack; got {stack!r}"
        )
        assert "blocked" in stack, (
            f"the logged stack must be the stuck worker's stack; got {stack!r}"
        )
        worker_name = getattr(record, "worker", None)
        assert isinstance(worker_name, str) and worker_name, (
            f"the ERROR must name the stuck worker; got {worker_name!r}"
        )
        assert "probe" in worker_name, (
            f"the worker extra must be the worker's own name; got {worker_name!r}"
        )
        elapsed_s = getattr(record, "elapsed_s", None)
        assert isinstance(elapsed_s, float), (
            f"the ERROR must carry the elapsed seconds as a float; got "
            f"{elapsed_s!r}"
        )
        assert elapsed_s == pytest.approx(round(elapsed, 1), abs=0.15), (
            f"the ERROR must carry the elapsed seconds (rounded to 1 decimal); "
            f"got {elapsed_s!r} for elapsed {elapsed!r}"
        )
    finally:
        release.set()


def test_run_with_watchdog_never_logs_the_stuck_frame_secrets(caplog):
    """Only ``plan_name`` leaves the stuck frame: no token, no other local."""
    daemon, _clock = make_daemon()
    release = threading.Event()
    try:

        def blocked():
            token = SECRET_TOKEN
            plan_name = "p-demo"
            release.wait(RELEASE_BOUND_S)
            return plan_name, token

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            health_mod.run_with_watchdog(
                daemon, blocked, timeout_s=JOIN_TIMEOUT_S, label="probe"
            )

        assert caplog.records, "the abandonment must log something"
        assert SECRET not in caplog.text, (
            "a local of the stuck frame must never reach a log record: the "
            "secret leaked into the captured log"
        )
        for record in caplog.records:
            for key, value in record.__dict__.items():
                if isinstance(value, str):
                    assert SECRET not in value, (
                        f"the secret leaked into the {key!r} field of a log "
                        f"record"
                    )
    finally:
        release.set()


def test_run_with_watchdog_logs_no_error_when_the_phase_completes(caplog):
    """A phase that finishes in time is not an abandonment: no ERROR."""
    daemon, _clock = make_daemon()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        completed, result = health_mod.run_with_watchdog(
            daemon, lambda: "done", timeout_s=5.0, label="probe"
        )

    assert completed is True and result == "done"
    assert not _error_records(caplog), (
        f"a completed phase must not log an abandonment ERROR; got "
        f"{[record.getMessage() for record in _error_records(caplog)]!r}"
    )


# ---------------------------------------------------------------------------
# The grace-window WARNING is rate limited
# ---------------------------------------------------------------------------

def test_grace_window_warning_is_rate_limited(monkeypatch, caplog):
    """One abandoned live worker: at most one WARNING per 30 seconds."""
    monkeypatch.setattr(
        daemon_mod, "_scan_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setattr(
        daemon_mod, "_reconcile_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    # The grace window must outlast the driven window, and the abandon-restart
    # hatch must stay out of the way: only the WARNING cadence is under test.
    monkeypatch.setenv(GRACE_ENV, "300")
    monkeypatch.setenv(THRESHOLD_ENV, "100")

    release = threading.Event()
    daemon, clock = make_daemon(scan_fn=WedgedScan(release))
    counts = []
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(RATE_LIMIT_WINDOW_S + 1):
                daemon.run_once()
                counts.append(len(_grace_warning_records(caplog)))
                clock.advance(1.0)

        assert counts[0] == 1, (
            "the FIRST grace-window warning must be kept; got "
            f"{counts[0]} after the first iteration"
        )
        assert counts[-1] <= 3, (
            f"the grace-window warning must be rate limited to one emission "
            f"per {WARNING_INTERVAL_S}s per worker; got {counts[-1]} "
            f"emissions over {RATE_LIMIT_WINDOW_S}s of daemon time"
        )
        warnings = _grace_warning_records(caplog)
        assert warnings, "the grace-window warning must be emitted"
        message = warnings[0].getMessage()
        assert "grace window" in message, (
            f"the warning must still name the grace window; got {message!r}"
        )
        assert "still alive" in message, (
            f"the warning must still name the abandoned worker; got {message!r}"
        )
    finally:
        release.set()


def test_grace_window_warning_is_rate_limited_per_worker(monkeypatch, caplog):
    """A second abandoned worker keeps its own first warning.

    The throttle is PER WORKER: the second worker is announced on the tick
    that abandons it, even though the first worker was announced one second
    earlier. A daemon-wide throttle would swallow it, and no throttle at all
    would announce the first worker twice.
    """
    monkeypatch.setattr(
        daemon_mod, "_scan_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setattr(
        daemon_mod, "_reconcile_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setenv(GRACE_ENV, "300")
    monkeypatch.setenv(THRESHOLD_ENV, "100")

    release = threading.Event()
    daemon, clock = make_daemon(scan_fn=WedgedScan(release, wedge_calls={1, 2}))
    counts = []
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(2):
                daemon.run_once()
                counts.append(len(_grace_warning_records(caplog)))
                clock.advance(1.0)

        assert counts[0] == 1, (
            "the first abandoned worker must be announced once; got "
            f"{counts[0]} warnings"
        )
        assert counts[1] == 2, (
            "the throttle must be per worker: the second abandoned worker "
            "keeps its own first warning; got "
            f"{counts[1]} warnings after two abandonments"
        )
    finally:
        release.set()


def test_grace_exit_error_is_not_rate_limited(monkeypatch, caplog):
    """The final grace-exit ERROR is loud and must survive the rate limit."""
    monkeypatch.setattr(
        daemon_mod, "_scan_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setattr(
        daemon_mod, "_reconcile_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setenv(GRACE_ENV, "2")
    monkeypatch.setenv(THRESHOLD_ENV, "100")

    release = threading.Event()
    daemon, clock = make_daemon(scan_fn=WedgedScan(release))
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            daemon.run_once()
            clock.advance(3.0)
            with pytest.raises(SystemExit):
                daemon.run_once()

        errors = [
            record
            for record in caplog.records
            if record.levelno == logging.ERROR
            and "grace window" in record.getMessage()
        ]
        assert errors, (
            "the grace-window exit must still be logged at ERROR; got "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )
    finally:
        release.set()
