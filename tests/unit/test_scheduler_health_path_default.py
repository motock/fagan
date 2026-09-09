"""run_daemon() must default the health file to PLAN_DIR / ".scheduler_health.json".

GOAL: make the fingerprint (scheduler health file) available by default.

Before this story, run_daemon() computed::

    health_path = os.environ.get("PIPELINE_SCHEDULER_HEALTH_PATH") or None

so with the env var unset, no health file was ever written. After this story,
an unset (or empty) PIPELINE_SCHEDULER_HEALTH_PATH must fall back to
``PLAN_DIR / ".scheduler_health.json"`` -- the same per-plan-store isolation
the singleton lock at ``PLAN_DIR / ".scheduler_daemon.lock"`` already enjoys
(two daemons on different plan stores keep independent health files by
construction). An explicit env value still wins.

These tests never let a real daemon loop start: SchedulerDaemon is replaced
by a fake that records its constructor kwargs and returns from run_forever()
immediately.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import ClassVar

import pytest

import pipeline.paths as paths_mod
import pipeline.scheduler_daemon as mod
import pipeline.server as server_mod

HEALTH_FILENAME = ".scheduler_health.json"
LOCK_FILENAME = ".scheduler_daemon.lock"


class FakeSchedulerDaemon:
    """Stand-in for the real SchedulerDaemon that never loops for real."""

    instances: ClassVar[list[FakeSchedulerDaemon]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.run_forever_calls: list = []
        FakeSchedulerDaemon.instances.append(self)

    def run_forever(self, stop_event) -> None:
        self.run_forever_calls.append(stop_event)


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    """Patch every collaborator run_daemon() wires up, plus PLAN_DIR."""
    FakeSchedulerDaemon.instances = []
    # run_daemon() reads PLAN_DIR as its own module global (for the lock path
    # and, after this story, the default health path); patch that binding.
    # pipeline.paths.PLAN_DIR is patched too so the test is honest about the
    # brief ("pipeline.paths.PLAN_DIR patched to tmp_path") and so either
    # spelling of the default expression resolves to tmp_path.
    monkeypatch.setattr(mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(mod, "SchedulerDaemon", FakeSchedulerDaemon)
    monkeypatch.setattr(mod, "build_bus", lambda: object())
    monkeypatch.setattr(mod, "scan_all_plans", lambda bus: None)
    monkeypatch.setattr(server_mod, "advance_all_plans", lambda: None)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)
    return tmp_path


def _constructed_daemon() -> FakeSchedulerDaemon:
    assert len(FakeSchedulerDaemon.instances) == 1, (
        f"expected exactly one SchedulerDaemon construction, got "
        f"{len(FakeSchedulerDaemon.instances)}"
    )
    return FakeSchedulerDaemon.instances[0]


def test_health_path_defaults_to_plan_dir_scheduler_health_json(daemon_env):
    """PIPELINE_SCHEDULER_HEALTH_PATH unset -> PLAN_DIR / ".scheduler_health.json"."""
    result = mod.run_daemon()
    assert result == 0

    daemon = _constructed_daemon()
    assert "health_path" in daemon.kwargs, (
        "run_daemon() must still pass health_path to the SchedulerDaemon "
        "constructor as a keyword"
    )
    passed = daemon.kwargs["health_path"]
    assert passed is not None, (
        "with PIPELINE_SCHEDULER_HEALTH_PATH unset, run_daemon() must default "
        f"the health path to PLAN_DIR / {HEALTH_FILENAME!r}, not pass None"
    )
    # Accept str or Path: assert against whatever run_daemon passes.
    assert Path(passed) == daemon_env / HEALTH_FILENAME

    # The fake's run_forever returns immediately: exactly one construction,
    # one run_forever call, no real loop ever started.
    assert len(daemon.run_forever_calls) == 1


def test_explicit_health_path_env_wins_over_default(daemon_env, monkeypatch):
    """A non-empty PIPELINE_SCHEDULER_HEALTH_PATH beats the PLAN_DIR default."""
    explicit = daemon_env / "custom_health" / "fingerprint.json"
    monkeypatch.setenv("PIPELINE_SCHEDULER_HEALTH_PATH", str(explicit))

    result = mod.run_daemon()
    assert result == 0

    daemon = _constructed_daemon()
    passed = daemon.kwargs["health_path"]
    assert passed is not None
    assert Path(passed) == explicit
    # ...and it is not the default.
    assert Path(passed) != daemon_env / HEALTH_FILENAME


def test_empty_health_path_env_behaves_as_unset(daemon_env, monkeypatch):
    """Empty string must fall back to the default, not become a literal '' path."""
    monkeypatch.setenv("PIPELINE_SCHEDULER_HEALTH_PATH", "")

    result = mod.run_daemon()
    assert result == 0

    daemon = _constructed_daemon()
    passed = daemon.kwargs["health_path"]
    assert passed is not None, (
        "PIPELINE_SCHEDULER_HEALTH_PATH='' must behave as unset and fall back "
        "to the PLAN_DIR default, not be passed through as an empty/None path"
    )
    assert Path(passed) == daemon_env / HEALTH_FILENAME


def test_default_health_path_is_isolated_per_plan_store_like_the_lock(daemon_env):
    """The default lives beside the singleton lock under the patched PLAN_DIR."""
    result = mod.run_daemon()
    assert result == 0

    # The singleton lock is still acquired at PLAN_DIR / ".scheduler_daemon.lock"
    # (os.open(..., O_CREAT) leaves the file behind) -- the lock logic is untouched.
    assert (daemon_env / LOCK_FILENAME).exists(), (
        "run_daemon() must still acquire its singleton lock at "
        f"PLAN_DIR / {LOCK_FILENAME!r}"
    )
    # The default health file is a sibling with its own name, so two daemons on
    # different plan stores keep independent health files by construction.
    assert (daemon_env / HEALTH_FILENAME) != (daemon_env / LOCK_FILENAME)
    daemon = _constructed_daemon()
    assert Path(daemon.kwargs["health_path"]) == daemon_env / HEALTH_FILENAME


def test_scheduler_daemon_constructor_and_write_health_unchanged():
    """DO-NOT guard: the constructor still takes health_path; write_health stays."""
    params = inspect.signature(mod.SchedulerDaemon.__init__).parameters
    assert "health_path" in params, (
        "SchedulerDaemon's constructor signature must not change: health_path "
        "must remain an accepted parameter"
    )
    for name in ("reconcile_fn", "scan_fn", "bus", "interval_s"):
        assert name in params
    # write_health is untouched too.
    assert callable(getattr(mod.SchedulerDaemon, "write_health", None)), (
        "SchedulerDaemon.write_health must still exist"
    )