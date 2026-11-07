"""Startup wiring and health-file behaviour of ``pipeline.scheduler_daemon``.

Found by a mutation pass over the module: each of these behaviours could be
removed or shifted by one without any existing test noticing.

* ``run_daemon`` must start the event consumers (a daemon that scans but never
  consumes events processes nothing) and stop them after the loop.
* An interval of exactly one second is valid; zero is the first invalid value.
* A second instance must say which lock it lost, not just exit 1.
* The health file written at the end of a tick must reflect that tick's
  abandon-streak reset, not the previous tick's streak.
* Heartbeat writes are paced by the heartbeat interval; they must not spin.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import scheduler_daemon as mod
from pipeline import server as server_mod
from pipeline.events import InProcessEventBus

HEARTBEAT_ENV = "PIPELINE_SCHEDULER_HEARTBEAT_SECONDS"
SCAN_JOIN_ENV = "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS"


class _RecordingDaemon:
    instances: ClassVar[list[_RecordingDaemon]] = []
    events: ClassVar[list[str]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        _RecordingDaemon.instances.append(self)

    def run_forever(self, _stop_event: object) -> None:
        _RecordingDaemon.events.append("run_forever")


@pytest.fixture
def daemon_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the collaborators ``run_daemon`` wires up; nothing loops or touches a broker."""
    _RecordingDaemon.instances = []
    _RecordingDaemon.events = []
    sentinel_bus = object()
    consumer_buses: list[object] = []

    def start_consumers(bus: object) -> None:
        _RecordingDaemon.events.append("start_consumers")
        consumer_buses.append(bus)

    monkeypatch.setattr(mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(mod, "SchedulerDaemon", _RecordingDaemon)
    monkeypatch.setattr(mod, "build_bus", lambda: sentinel_bus)
    monkeypatch.setattr(mod, "scan_all_plans", lambda bus: None)
    monkeypatch.setattr(mod, "start_consumers", start_consumers)
    monkeypatch.setattr(
        mod, "stop_consumers", lambda: _RecordingDaemon.events.append("stop_consumers")
    )
    monkeypatch.setattr(server_mod, "advance_all_plans", lambda: None)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)
    return {"tmp_path": tmp_path, "bus": sentinel_bus, "consumer_buses": consumer_buses}


def _hold_lock(tmp_path: Path) -> int:
    fd = os.open(tmp_path / ".scheduler_daemon.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


# ---------------------------------------------------------------------------
# run_daemon: consumers
# ---------------------------------------------------------------------------


def test_should_start_consumers_before_the_loop_and_stop_them_after(daemon_env: dict) -> None:
    assert mod.run_daemon() == 0

    assert _RecordingDaemon.events == ["start_consumers", "run_forever", "stop_consumers"]


def test_should_start_consumers_on_the_bus_the_daemon_scans(daemon_env: dict) -> None:
    mod.run_daemon()

    assert daemon_env["consumer_buses"] == [daemon_env["bus"]]
    assert _RecordingDaemon.instances[0].kwargs["bus"] is daemon_env["bus"]


def test_should_not_start_consumers_when_another_instance_holds_the_lock(
    daemon_env: dict,
) -> None:
    fd = _hold_lock(daemon_env["tmp_path"])
    try:
        result = mod.run_daemon()
    finally:
        _release_lock(fd)

    assert result == 1
    assert _RecordingDaemon.events == []


# ---------------------------------------------------------------------------
# run_daemon: interval boundary and lock message
# ---------------------------------------------------------------------------


def test_should_accept_an_interval_of_exactly_one_second(
    daemon_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_INTERVAL_S", "1")

    assert mod.run_daemon() == 0
    assert _RecordingDaemon.instances[0].kwargs["interval_s"] == 1


def test_should_name_the_lock_when_another_instance_already_holds_it(
    daemon_env: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    fd = _hold_lock(daemon_env["tmp_path"])
    try:
        mod.run_daemon()
    finally:
        _release_lock(fd)

    err = capsys.readouterr().err
    assert "another instance already holds the lock" in err
    assert ".scheduler_daemon.lock" in err


# ---------------------------------------------------------------------------
# SchedulerDaemon: health file and heartbeat pacing
# ---------------------------------------------------------------------------


def _real_daemon(health: Path, scan_fn) -> mod.SchedulerDaemon:
    return mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=scan_fn,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
        clock=time.monotonic,
        health_path=str(health),
    )


def test_should_write_the_streak_reset_into_the_health_file_at_the_end_of_a_healthy_tick(
    tmp_path: Path,
) -> None:
    health = tmp_path / "health.json"
    daemon = _real_daemon(health, lambda: "ok")
    daemon._consecutive_abandons = 2
    daemon.write_health(str(health))
    assert json.loads(health.read_text())["consecutive_abandonments"] == 2

    daemon.run_once()

    assert "consecutive_abandonments" not in json.loads(health.read_text())


def test_should_pace_heartbeat_writes_by_the_heartbeat_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SCAN_JOIN_ENV, "10.0")
    monkeypatch.setenv(HEARTBEAT_ENV, "0.1")
    daemon = _real_daemon(tmp_path / "health.json", lambda: time.sleep(0.5))
    writes: list[None] = []
    # A free write: the real one fingerprints the config and takes longer than
    # the heartbeat interval, which would itself pace the loop and hide a
    # missing wait between slices.
    daemon.write_health = lambda _path: writes.append(None)

    started = time.monotonic()
    daemon.run_once()
    elapsed = time.monotonic() - started

    # One write per 0.1s slice plus the end-of-tick write, with slack. A loop
    # that stopped waiting between slices would write thousands of times.
    assert len(writes) <= elapsed / 0.1 + 3
