"""A freshly started scheduler must replace the health file immediately.

``SchedulerDaemon.start()`` runs the startup reconcile through the watchdog
but (before this story) wrote health only inside ``run_once``, after the scan
phase. A restarted process therefore left the previous process's health file
on disk -- possibly carrying a dead pid and a stale ``last_error`` such as
``reconcile_fn stalled past the join deadline`` -- for up to a full interval.
``start()`` must write the health file BEFORE the startup reconcile runs, so
any reader sees the new process's pid and a null ``last_error`` immediately.
The startup write is guarded exactly like the post-scan write: a failing
health write must never kill startup, and a raising reconcile still
propagates.
"""

import json
import logging
import os

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus


def make_daemon(reconcile_fn, health_path):
    return mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
        health_path=health_path,
    )


def test_start_writes_health_before_reconcile_runs(tmp_path):
    """At reconcile call time the on-disk health file already belongs to
    THIS process: config.pid == os.getpid() and last_error is null, even
    though a stale file from a dead process (pid 1, last_error 'stalled')
    was on disk before start()."""
    health_path = tmp_path / "health.json"
    # Pre-seed the previous process's health file: dead pid, stale error.
    health_path.write_text(
        json.dumps(
            {
                "alive": True,
                "last_reconcile_ts": "2026-10-08T00:00:00+00:00",
                "last_scan_ts": None,
                "last_error": "reconcile_fn stalled past the join deadline",
                "reconcile_count": 41,
                "scan_count": 41,
                "reconcile_timed_out": 1,
                "config": {"pid": 1},
            }
        )
    )
    seen = {}
    calls = []

    def reconcile():
        calls.append(1)
        assert health_path.exists(), (
            "no health file on disk when the startup reconcile ran: start() "
            "still writes health only later (inside run_once)"
        )
        with open(health_path) as fh:
            seen.update(json.load(fh))

    daemon = make_daemon(reconcile, str(health_path))
    daemon.start()

    assert len(calls) == 1, "start() must call reconcile exactly once"
    assert seen["config"]["pid"] == os.getpid(), (
        "the health file on disk during the startup reconcile still names "
        "another pid: a freshly started scheduler must replace the previous "
        "process's health file before the reconcile runs"
    )
    assert seen["last_error"] is None, (
        "the health file on disk during the startup reconcile still carries "
        "the dead process's last_error: the stale 'stalled' error must be "
        "replaced before the reconcile runs"
    )


def test_start_without_health_path_writes_nothing(monkeypatch, tmp_path):
    """health_path=None: start() calls reconcile once and never writes
    health (the startup write stays gated on a configured path)."""
    writes = []
    monkeypatch.setattr(
        mod.SchedulerDaemon,
        "write_health",
        lambda self, path: writes.append(path),
    )
    calls = []

    def reconcile():
        calls.append(1)

    daemon = make_daemon(reconcile, None)
    daemon.start()

    assert len(calls) == 1, "start() must call reconcile exactly once"
    assert writes == [], "start() wrote health even though health_path is None"
    assert list(tmp_path.iterdir()) == []


def test_start_survives_a_failing_startup_health_write(tmp_path, caplog):
    """A health write that fails at the filesystem boundary (unwritable
    directory) must not kill startup: reconcile still runs exactly once
    and the failure is logged."""
    # No such directory: opening <path>.tmp for writing raises OSError.
    health_path = tmp_path / "no_such_dir" / "health.json"
    calls = []

    def reconcile():
        calls.append(1)

    daemon = make_daemon(reconcile, str(health_path))
    with caplog.at_level(logging.ERROR):
        daemon.start()  # must not raise

    assert len(calls) == 1, (
        "a failing startup health write must not prevent the startup "
        "reconcile from running exactly once"
    )
    assert any(
        "write_health failed at startup" in r.getMessage() for r in caplog.records
    ), "the failed startup health write must be logged"


def test_start_still_propagates_a_raising_reconcile(tmp_path):
    """start() does not swallow exceptions: a raising reconcile_fn still
    propagates to the caller (the startup health write changes nothing
    about error propagation)."""
    health_path = tmp_path / "health.json"

    def reconcile():
        raise RuntimeError("boom")

    daemon = make_daemon(reconcile, str(health_path))
    with pytest.raises(RuntimeError, match="boom"):
        daemon.start()
