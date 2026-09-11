"""Abandon-restart escape hatch tests for ``pipeline.scheduler_daemon``.

Story LOCKSTARVE-C2. Measured live on 2026-09-11: the daemon abandoned three
scan workers; one held ``~/.claude/plans/chat-logs-usage.lock``. The next
reconcile then blocked on that leaked flock forever — zero CPU, frozen health
file, every other plan starved — until a human ran ``launchctl kickstart -k``.
C1 bounds the reconcile phase so the LOOP survives, but the leaked lock is
still never released: after C1 every subsequent tick re-blocks on a lock no
live thread owns, which is quieter and therefore harder to notice. Process
death is the only thing that releases a flock, and the launchd job
``com.claude.pipeline.advance-scheduler`` has ``KeepAlive=true``, so exiting
IS the recovery.

Contract pinned here:

* ``_abandon_restart_threshold()`` reads
  ``PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD`` per call (never cached), default
  3, malformed -> warn + default. ``<= 0`` means DISABLED (never restart) —
  deliberately NOT "use the default", so an operator can turn the hatch off.
* The daemon tracks CONSECUTIVE abandonments (scan OR reconcile — C1 routes
  both through ``_run_with_watchdog``). Any completed phase resets the streak:
  abandonments spread across a healthy day must never trigger a restart.
* When the streak reaches the threshold, at the END of ``run_once`` — AFTER
  the health file is written, so the final state is observable on disk — the
  daemon logs at ERROR naming the count/threshold and the suspected leaked
  plan lock, then raises ``SystemExit(1)`` so launchd restarts the process.
  ``os._exit`` is forbidden (it would skip the health write). ``run_forever``
  must let the ``SystemExit`` propagate.
* ``health()`` exposes the new counter ONLY once it is non-zero (the additive,
  gated pattern used for ``scan_timed_out``), so a clean tick's key set stays
  byte-for-byte unchanged. These tests assert membership/absence, never an
  exact total key set.

RED by design until the LOCKSTARVE-C2 production edit lands: the tests below
fail on a missing ``_abandon_restart_threshold``, a never-raised
``SystemExit``, an unexposed streak counter, an unextended KNOWN LIMITATION
docstring, and a missing REFERENCE.md row. Tests 5 and 6 (disabled
thresholds) are vacuous guards against the pre-C2 code — they pin that the
hatch, once added, never fires when the operator disables it.
"""
import ast
import json
import logging
import pathlib
import threading

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

ENV_VAR = "PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"
DEFAULT_THRESHOLD = 3
# The join deadline the fakes run against: short enough that an abandoned
# worker costs ~0.05s, long enough to be deterministic on a loaded CI box.
JOIN_TIMEOUT_S = 0.05
# A wedged worker is always released eventually so leaked threads can never
# outlive the test process even if an assertion fires mid-test.
RELEASE_BOUND_S = 30.0
# Generous wall-clock bound for run_once()/run_forever() once the (short)
# join deadline fires; converts a would-be suite hang into a loud failure.
RUN_ONCE_BOUND_S = 6.0

# Gated health keys that ALREADY exist (sh-02 + LOCKSTARVE-C1) and therefore
# must not be mistaken for this story's new counter when diffing key sets.
PRE_EXISTING_GATED_KEYS = frozenset(
    {"scan_timed_out", "last_scan_timeout_ts", "last_reconcile_timeout_ts"}
)
# Plausible names for the new counter, used only for the clean-tick absence
# assertion. The real name is deliberately NOT pinned: the new counter is
# identified name-agnostically as "a key that appears in health() only once
# the streak is non-zero" (see test_clean_tick_health_...).
CANDIDATE_COUNTER_NAMES = frozenset(
    {
        "consecutive_abandonments",
        "consecutive_abandonment_count",
        "abandon_streak",
        "abandonment_streak",
        "abandon_restart_streak",
        "abandonments",
        "abandoned_workers",
        "consecutive_abandoned_workers",
    }
)


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/unit/test_scheduler_daemon_scan_watchdog.py's seams)
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
    start=0.0,
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
        target=_target, daemon=True, name="abandon-restart-helper"
    )
    helper.start()
    helper.join(bound)
    if helper.is_alive():
        raise AssertionError(
            f"{what} did not return within {bound:g}s wall clock; the "
            f"abandon-restart path must never hang the loop"
        )
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def run_once_bounded(daemon, bound=RUN_ONCE_BOUND_S):
    return _run_on_thread(daemon.run_once, bound, "run_once()")


def run_forever_bounded(daemon, stop_event, bound=RUN_ONCE_BOUND_S):
    try:
        return _run_on_thread(
            lambda: daemon.run_forever(stop_event), bound, "run_forever()"
        )
    except AssertionError:
        stop_event.set()  # let a swallowed-SystemExit loop wind down
        raise


def run_once_expect_normal(daemon):
    """``run_once`` must return normally — a premature SystemExit fails loud."""
    try:
        return run_once_bounded(daemon)
    except SystemExit as exc:
        pytest.fail(
            f"run_once raised SystemExit({exc.code!r}) before the consecutive "
            f"abandonment streak reached the threshold: the abandon-restart "
            f"escape hatch fired prematurely"
        )


def patch_join_timeouts(monkeypatch):
    """Make both watchdog phases abandon after JOIN_TIMEOUT_S instead of 900s."""
    monkeypatch.setattr(
        mod, "_scan_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )
    monkeypatch.setattr(
        mod, "_reconcile_join_timeout_seconds", lambda: JOIN_TIMEOUT_S
    )


def _module_source() -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


def _new_health_keys(health_before, health_after):
    """Keys health() grew after abandonments, excluding pre-existing gates."""
    return (
        set(health_after)
        - set(health_before)
        - PRE_EXISTING_GATED_KEYS
    )


# ---------------------------------------------------------------------------
# Positive: the escape hatch fires exactly at the threshold
# ---------------------------------------------------------------------------

def test_below_threshold_abandonments_return_normally_and_expose_streak(
    monkeypatch,
):
    """(Positive 1) threshold-1 consecutive abandonments: no SystemExit.

    Also pins the additive half of the health() gating rule: once the streak
    is non-zero it MUST be observable, under a key that a clean tick does not
    have (name-agnostic; see test_clean_tick_health_...).
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "2")
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        clean_health = daemon.health()
        result = run_once_expect_normal(daemon)
        assert result == {"scanned": False, "reconciled": False}
        assert scan.calls == 1

        after_health = daemon.health()
        new_keys = _new_health_keys(clean_health, after_health)
        assert new_keys, (
            "after an abandonment the consecutive-abandonment streak must be "
            f"exposed in health() under a new key; clean keys="
            f"{sorted(clean_health)}, after={sorted(after_health)}"
        )
        assert any(
            isinstance(after_health[k], int)
            and not isinstance(after_health[k], bool)
            and after_health[k] >= 1
            for k in new_keys
        ), f"the new streak key must be an int >= 1: {after_health}"
    finally:
        release.set()


def test_threshold_th_abandonment_raises_systemexit_1(monkeypatch, caplog):
    """(Positive 2) the threshold-th abandonment raises SystemExit(1).

    Also pins: the reconcile phase's abandonment counts toward the streak
    (C1 routes both phases through _run_with_watchdog); the restart decision
    is logged at ERROR naming a leaked plan lock as the suspected cause; and
    run_forever lets the SystemExit propagate out of run_once.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "2")
    release = threading.Event()
    # Tick 1: scan completes, reconcile (due) wedges -> abandonment #1.
    # Tick 2: reconcile no longer due, scan wedges -> abandonment #2 -> exit.
    scan = WedgedCall(release, wedge_on={2})
    reconcile = WedgedCall(release, wedge_on={1})
    daemon, clock = make_daemon(
        scan_fn=scan, reconcile_fn=reconcile, interval_s=60
    )
    try:
        clock.advance(60)  # make reconcile due on tick 1
        with caplog.at_level(
            logging.ERROR, logger="pipeline.scheduler_daemon"
        ):
            run_once_expect_normal(daemon)  # streak 1 < 2
            with pytest.raises(SystemExit) as exc_info:
                run_once_bounded(daemon)  # streak 2 -> restart
        assert exc_info.value.code == 1, (
            f"the escape hatch must exit 1 so launchd restarts the daemon; "
            f"got {exc_info.value.code!r}"
        )
        restart_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR and "lock" in r.getMessage().lower()
        ]
        assert restart_msgs, (
            "reaching the threshold must log at ERROR naming a leaked plan "
            "lock as the suspected cause"
        )
        assert any("2" in m for m in restart_msgs), (
            "the restart ERROR must name the count/threshold (2 here)"
        )
    finally:
        release.set()

    # run_forever must let the SystemExit propagate out of run_once rather
    # than catching it. Threshold 1: the very first abandonment must kill the
    # loop. signal is patched because run_forever runs on a helper thread.
    monkeypatch.setenv(ENV_VAR, "1")
    registered = []
    monkeypatch.setattr(mod, "signal", lambda sig, handler: registered.append(sig))
    release2 = threading.Event()
    daemon2, _clock2 = make_daemon(scan_fn=WedgedCall(release2))
    stop_event = threading.Event()
    try:
        with pytest.raises(SystemExit) as forever_exc:
            run_forever_bounded(daemon2, stop_event)
        assert forever_exc.value.code == 1, (
            "run_forever must propagate the abandon-restart SystemExit(1) "
            "instead of swallowing it"
        )
    finally:
        release2.set()


def test_health_file_written_before_systemexit(monkeypatch, tmp_path):
    """(Positive 3) the health file is written BEFORE the SystemExit."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "2")
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    health_path = tmp_path / "health.json"
    daemon, _clock = make_daemon(scan_fn=scan, health_path=str(health_path))
    try:
        run_once_expect_normal(daemon)  # streak 1; health written (scan_count 1)
        with pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # streak 2 -> restart
        assert exc_info.value.code == 1

        assert health_path.exists(), (
            "the health file must be written BEFORE the SystemExit is raised "
            "so the final state is observable on disk"
        )
        data = json.loads(health_path.read_text(encoding="utf-8"))
        assert data["scan_count"] == 2, (
            "the on-disk health must reflect the FINAL tick (the one that "
            "triggered the restart), not the previous one"
        )
        assert data["alive"] is True
        assert isinstance(data["last_error"], str) and "abandon" in data[
            "last_error"
        ].lower(), (
            "the final tick's abandonment must be recorded in last_error"
        )
    finally:
        release.set()


def test_completed_phase_resets_consecutive_streak(monkeypatch):
    """(Positive 4) abandon, abandon, complete, abandon, abandon -> no exit.

    The trailing sixth abandonment then proves the reset happened: without a
    reset the fourth abandonment would already hit the threshold of 3.

    Only the scan phase ever abandons here (one abandoned worker per tick, so
    the count is the same whether the implementation increments per worker or
    per tick); the reconcile phase's contribution to the streak is pinned
    separately by test_threshold_th_abandonment_raises_systemexit_1.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "3")
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={1, 2, 4, 5, 6})
    reconcile = Counter()
    # interval_s=60 and a clock that never advances past the last reconcile
    # keep the reconcile phase out of every tick: _last_reconcile is pinned
    # to the clock value at init/last reconcile, so it is never due.
    daemon, _clock = make_daemon(
        scan_fn=scan, reconcile_fn=reconcile, interval_s=60
    )
    try:
        # Ticks 1-2: consecutive abandonments -> streak 2.
        run_once_expect_normal(daemon)
        run_once_expect_normal(daemon)
        # Tick 3: scan completes -> the streak resets to 0.
        assert daemon.run_once() == {"scanned": True, "reconciled": False}
        # Ticks 4-5: two more abandonments -> streak 2, still below 3. With a
        # cumulative (non-resetting) counter this would already be 4 and the
        # fourth tick would have raised.
        run_once_expect_normal(daemon)
        run_once_expect_normal(daemon)
        # Tick 6: third CONSECUTIVE abandonment -> the hatch finally fires.
        with pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)
        assert exc_info.value.code == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Negative / boundary: the operator can disable the hatch
# ---------------------------------------------------------------------------

def test_zero_threshold_disables_restart(monkeypatch):
    """(Negative 5) PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD=0 disables it."""
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "0")
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        for i in range(10):
            result = run_once_expect_normal(daemon)
            assert result == {"scanned": False, "reconciled": False}, (
                f"tick {i + 1}: a disabled hatch must still abandon the "
                f"wedged worker and continue the loop"
            )
        assert scan.calls == 10
    finally:
        release.set()


def test_negative_threshold_disables_rather_than_defaulting(monkeypatch):
    """(Negative 6) -1 behaves like 0 (disabled), NOT as the default 3.

    Ten consecutive abandonments must survive: had -1 been treated as "use
    the default", the third abandonment would already have raised SystemExit.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "-1")
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        for _ in range(10):
            run_once_expect_normal(daemon)
        assert scan.calls == 10
    finally:
        release.set()


def test_malformed_threshold_degrades_to_default_with_warning(
    monkeypatch, caplog
):
    """(Negative 7) "abc" degrades to the default 3 and emits a warning.

    Mirrors _scan_join_timeout_seconds' idiom: read per call (never cached),
    malformed -> warn + default. The behavioural half proves "abc" really
    behaves as 3: two abandonments survive, the third raises SystemExit.
    """
    helper = getattr(mod, "_abandon_restart_threshold", None)
    assert helper is not None, (
        "pipeline.scheduler_daemon must define _abandon_restart_threshold() "
        "reading PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"
    )
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert helper() == DEFAULT_THRESHOLD, (
        "unset env must yield the default threshold of 3"
    )
    monkeypatch.setenv(ENV_VAR, "7")
    assert helper() == 7, "the threshold must be read per call, never cached"
    monkeypatch.setenv(ENV_VAR, "9")
    assert helper() == 9, "the threshold must be re-read on every call"

    monkeypatch.setenv(ENV_VAR, "abc")
    with caplog.at_level(logging.WARNING, logger="pipeline.scheduler_daemon"):
        assert helper() == DEFAULT_THRESHOLD, (
            "a malformed value must degrade to the default 3, not crash or "
            "disable the hatch"
        )
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and ENV_VAR in r.getMessage()
    ]
    assert warnings, (
        "a malformed threshold must emit a warning naming the env var"
    )

    # Behavioural: "abc" behaves exactly like the default 3.
    patch_join_timeouts(monkeypatch)
    release = threading.Event()
    scan = WedgedCall(release)  # wedge every call
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # streak 1
        run_once_expect_normal(daemon)  # streak 2
        with pytest.raises(SystemExit) as exc_info:
            run_once_bounded(daemon)  # streak 3 == default threshold
        assert exc_info.value.code == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Negative: health() gating (additive-only, never an exact key set)
# ---------------------------------------------------------------------------

def test_clean_tick_health_does_not_expose_abandon_counter(monkeypatch):
    """(Negative 8) a clean tick's health() must not contain the new key.

    Membership/absence assertions only — never an exact total key set, so a
    later sibling adding another field cannot break this test. The counter is
    identified name-agnostically as a key that appears only once the streak
    is non-zero; plausible names are additionally asserted absent on a clean
    tick.
    """
    patch_join_timeouts(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "2")
    release = threading.Event()
    scan = WedgedCall(release, wedge_on={2})  # tick 1 clean, tick 2 wedged
    daemon, _clock = make_daemon(scan_fn=scan)
    try:
        run_once_expect_normal(daemon)  # clean tick
        clean_health = daemon.health()
        assert not (CANDIDATE_COUNTER_NAMES & set(clean_health)), (
            "a clean tick's health() must not expose the abandonment streak "
            f"counter; saw {sorted(CANDIDATE_COUNTER_NAMES & set(clean_health))}"
        )

        run_once_expect_normal(daemon)  # abandonment #1 (streak 1 < 2)
        after_health = daemon.health()
        new_keys = _new_health_keys(clean_health, after_health)
        assert new_keys, (
            "the streak counter must be GATED: absent on a clean tick but "
            f"present once non-zero; clean={sorted(clean_health)}, "
            f"after={sorted(after_health)}"
        )
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Source / documentation pins
# ---------------------------------------------------------------------------

def test_known_limitation_docstring_preserved_and_reference_row_added():
    """(Negative 9) the KNOWN LIMITATION survives and points at the hatch.

    Also pins: the env var name exists in the module, ``os._exit`` is NOT
    used (it would skip the health write), and REFERENCE.md documents the env
    var as a table row anchored next to PIPELINE_MERGE_MAX_ATTEMPTS.
    """
    source = _module_source()
    assert source.count("KNOWN LIMITATION") >= 1, (
        "the KNOWN LIMITATION text must survive in pipeline/scheduler_daemon.py"
    )
    assert ENV_VAR in source, (
        "the escape hatch must be controlled by "
        "PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"
    )
    assert "os._exit" not in source, (
        "os._exit would skip the health write; the hatch must raise "
        "SystemExit(1) instead"
    )

    tree = ast.parse(source)
    scan_doc = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_scan_with_watchdog"
        ):
            scan_doc = ast.get_docstring(node)
    assert scan_doc is not None, (
        "_scan_with_watchdog must still exist with its docstring"
    )
    assert "KNOWN LIMITATION" in scan_doc and "_plan_lock" in scan_doc, (
        "the KNOWN LIMITATION docstring (leaked _plan_lock on abandonment) "
        "must not be deleted or reworded"
    )
    assert ENV_VAR in scan_doc, (
        "the KNOWN LIMITATION docstring must be extended with a sentence "
        f"pointing at the new escape hatch ({ENV_VAR})"
    )

    repo_root = pathlib.Path(mod.__file__).resolve().parents[1]
    ref_path = repo_root / "REFERENCE.md"
    assert ref_path.exists(), "REFERENCE.md must exist at the repo root"
    lines = ref_path.read_text(encoding="utf-8").splitlines()
    row_idx = merge_idx = None
    row_text = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"| `{ENV_VAR}`") and row_idx is None:
            row_idx, row_text = i, stripped
        if (
            stripped.startswith("| `PIPELINE_MERGE_MAX_ATTEMPTS`")
            and merge_idx is None
        ):
            merge_idx = i
    assert row_idx is not None, (
        "REFERENCE.md must document PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD "
        "as a table row (| `NAME` | default | description |)"
    )
    assert merge_idx is not None, (
        "REFERENCE.md's PIPELINE_MERGE_MAX_ATTEMPTS row is the anchor"
    )
    assert abs(row_idx - merge_idx) <= 6, (
        "the new row must be anchored next to the PIPELINE_MERGE_MAX_ATTEMPTS "
        f"row (found {abs(row_idx - merge_idx)} lines away)"
    )
    cells = [c.strip() for c in row_text.split("|")]
    assert len(cells) >= 4, (
        "the row must match the surrounding rows' column shape "
        f"(name | default | description): {row_text!r}"
    )
    assert "`3`" in cells, (
        "the row must document the default threshold of 3: "
        f"{row_text!r}"
    )