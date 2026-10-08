"""Tests for the scheduler liveness heartbeat (story SCHED-HEARTBEAT).

While a scan or reconcile phase runs, the main thread blocks in a single
``worker.join(timeout_s)`` of up to 900s and health is written only after the
phase ends. The scan phase runs bus handlers synchronously (reviews, seconds
to minutes), so ``last_scan_ts`` and the health file's mtime freeze while the
daemon is healthy and busy — indistinguishable from a dead or wedged process.

These tests use REAL threads and tiny env values (heartbeat 0.05s); nothing
here mocks the daemon's internals.
"""
import json
import threading
import time
from datetime import datetime

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

HEARTBEAT_ENV = "PIPELINE_SCHEDULER_HEARTBEAT_SECONDS"
SCAN_JOIN_ENV = "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS"

# The seven keys health() returns on a clean tick (story-pinned; the heartbeat
# must never add to them — it lives in the FILE payload only).
PINNED_HEALTH_KEYS = {
    "alive",
    "last_reconcile_ts",
    "last_scan_ts",
    "last_error",
    "reconcile_count",
    "scan_count",
    "reconcile_timed_out",
}


def _make_daemon(scan_fn, health_path=None):
    return mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=scan_fn,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
        clock=time.monotonic,
        health_path=health_path,
    )


# ---------------------------------------------------------------------------
# _heartbeat_interval_seconds
# ---------------------------------------------------------------------------

def test_heartbeat_interval_defaults_when_unset(monkeypatch):
    monkeypatch.delenv(HEARTBEAT_ENV, raising=False)
    assert mod._heartbeat_interval_seconds() == 30.0


def test_heartbeat_interval_reads_env(monkeypatch):
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")
    assert mod._heartbeat_interval_seconds() == 0.05


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "nan", "inf"])
def test_heartbeat_interval_malformed_degrades_to_default(monkeypatch, raw):
    monkeypatch.setenv(HEARTBEAT_ENV, raw)
    # No exception, and the default is used rather than a value that would
    # disable the sliced join (non-finite) or busy-loop it (<= 0).
    assert mod._heartbeat_interval_seconds() == 30.0


def test_heartbeat_interval_is_reexported_from_daemon():
    from pipeline import scheduler_timeouts

    assert (
        mod._heartbeat_interval_seconds
        is scheduler_timeouts._heartbeat_interval_seconds
    )


# ---------------------------------------------------------------------------
# Positive: a long phase keeps refreshing the health file
# ---------------------------------------------------------------------------

def test_heartbeat_refreshes_health_file_during_long_scan(tmp_path, monkeypatch):
    monkeypatch.setenv(SCAN_JOIN_ENV, "5.0")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")
    health = tmp_path / "health.json"
    observed = []

    def scan_fn():
        deadline = time.monotonic() + 0.7
        while time.monotonic() < deadline:
            time.sleep(0.02)
            if health.exists():
                observed.append(
                    (health.stat().st_mtime_ns, json.loads(health.read_text()))
                )
        return "ok"

    daemon = _make_daemon(scan_fn, health_path=str(health))
    daemon.run_once()

    mtimes = [mt for mt, _payload in observed]
    assert len(set(mtimes)) >= 2, f"health mtime did not advance: {mtimes}"

    mid = [p for _mt, p in observed if p.get("phase") == "scan"]
    assert mid, "no mid-phase heartbeat payload observed"
    parsed = datetime.fromisoformat(mid[-1]["heartbeat_ts"])
    assert parsed.tzinfo is not None


def test_fast_worker_fires_no_heartbeat_writes(tmp_path, monkeypatch):
    monkeypatch.setenv(SCAN_JOIN_ENV, "5.0")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")
    health = tmp_path / "health.json"
    daemon = _make_daemon(lambda: "ok", health_path=str(health))

    phases = []
    orig = daemon.write_health

    def counting(path):
        phases.append(daemon._heartbeat_phase)
        return orig(path)

    daemon.write_health = counting
    daemon.run_once()

    # No heartbeat write fired: every write happened with the phase cleared.
    assert all(p is None for p in phases), phases
    payload = json.loads(health.read_text())
    assert "phase" not in payload
    assert "heartbeat_ts" not in payload


def test_health_file_has_no_heartbeat_keys_after_phase_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SCAN_JOIN_ENV, "2.0")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")
    health = tmp_path / "health.json"

    def scan_fn():
        time.sleep(0.3)
        return "ok"

    daemon = _make_daemon(scan_fn, health_path=str(health))
    daemon.run_once()

    payload = json.loads(health.read_text())
    assert "heartbeat_ts" not in payload
    assert "phase" not in payload


# ---------------------------------------------------------------------------
# Negative / boundary
# ---------------------------------------------------------------------------

def test_worker_outliving_deadline_is_abandoned_at_deadline(monkeypatch):
    monkeypatch.setenv(SCAN_JOIN_ENV, "0.4")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")
    release = threading.Event()

    def scan_fn():
        release.wait(5)

    daemon = _make_daemon(scan_fn)
    start = time.monotonic()
    try:
        daemon.run_once()
    finally:
        elapsed = time.monotonic() - start
        release.set()

    # Abandoned at ~the deadline: within one heartbeat slice plus slack, not
    # the worker's full 5s.
    assert 0.35 <= elapsed <= 0.55, elapsed
    assert daemon._consecutive_abandons == 1
    assert daemon._scan_timed_out == 1


def test_heartbeat_with_no_health_path_is_silent(monkeypatch):
    monkeypatch.setenv(SCAN_JOIN_ENV, "2.0")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.05")

    def scan_fn():
        time.sleep(0.3)

    daemon = _make_daemon(scan_fn, health_path=None)
    daemon.run_once()  # must not raise
    assert daemon._heartbeat_phase is None


def test_health_clean_tick_keeps_seven_pinned_keys():
    daemon = _make_daemon(lambda: None)
    assert set(daemon.health()) == PINNED_HEALTH_KEYS


def test_health_never_includes_heartbeat_keys():
    daemon = _make_daemon(lambda: None)
    daemon._heartbeat_phase = "scan"
    daemon._heartbeat_ts = "2026-01-01T00:00:00+00:00"
    snapshot = daemon.health()
    assert "phase" not in snapshot
    assert "heartbeat_ts" not in snapshot
    assert set(snapshot) == PINNED_HEALTH_KEYS
