"""SYS-02: the scheduler daemon's tick runs as ``system:scheduler``.

The daemon exists so the pipeline never depends on an external clock, and it
runs unattended: nothing in the process authenticated a human, so every action
it dispatches must be attributed to the system principal rather than to the
ambient local operator. ``pipeline.actor`` binds that principal for the
duration of one request, and a ContextVar bound on the main thread does NOT
propagate into the fresh ``threading.Thread`` the watchdog spawns for each
phase -- so the binding has to happen *inside* the worker.

These tests drive the real ``SchedulerDaemon`` (only the injected phase
callables are fakes) and pin:

* ``pipeline.actor`` re-exports ``SYSTEM_SCHEDULER`` without importing
  ``app.identity`` in the daemon (the daemon may import only stdlib and
  ``pipeline``);
* a ``reconcile_fn`` and a ``scan_fn`` each observe ``SYSTEM_SCHEDULER`` from
  inside their worker thread;
* the main thread's principal is unchanged after the run, and a second run
  rebinds again (the binding is per worker, never leaked to the caller);
* a raising phase still propagates its exception;
* ``run_daemon`` keeps wiring ``advance_all_plans`` itself as
  ``reconcile_fn`` (unchanged composition root).
"""

from __future__ import annotations

import threading

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401 (import-order idiom: import a pipeline module first)
from app.identity import SYSTEM_SCHEDULER, Principal
from pipeline import actor as actor_mod
from pipeline import scheduler_daemon as daemon_mod
from pipeline import server as server_mod
from pipeline.events import InProcessEventBus

#: A non-operator principal, bound on the main thread the way the dashboard
#: middleware binds one (tests/unit/test_patch_plan_enforcement.py idiom).
HUMAN_ALICE = Principal(subject="human:alice", auth_method="token")


class _FakeClock:
    """Monotonic clock stub the daemon can advance by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _daemon(reconcile_fn, scan_fn, *, interval_s: float = 60) -> object:
    """Build a real ``SchedulerDaemon`` around the injected phase callables."""
    return daemon_mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=InProcessEventBus(),
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=_FakeClock(),
    )


def test_actor_reexports_system_scheduler_without_app_identity_import():
    # getattr, not attribute access: a missing re-export must fail as an
    # assertion rather than raising AttributeError.
    assert getattr(actor_mod, "SYSTEM_SCHEDULER", None) is SYSTEM_SCHEDULER


def test_reconcile_fn_observes_system_scheduler_inside_worker_thread():
    seen: dict = {}

    def reconcile_fn():
        seen["principal"] = actor_mod.current_principal()
        seen["thread"] = threading.current_thread()
        return "reconciled"

    daemon = _daemon(reconcile_fn, lambda: None)
    daemon.start()

    assert seen["principal"] is SYSTEM_SCHEDULER
    # The phase really ran on the watchdog's worker thread, not the caller's.
    assert seen["thread"] is not threading.current_thread()


def test_scan_fn_observes_system_scheduler_inside_worker_thread():
    seen: dict = {}

    def scan_fn():
        seen["principal"] = actor_mod.current_principal()
        seen["thread"] = threading.current_thread()

    daemon = _daemon(lambda: None, scan_fn)
    daemon.run_once()

    assert seen["principal"] is SYSTEM_SCHEDULER
    assert seen["thread"] is not threading.current_thread()


def test_main_thread_principal_unchanged_and_rebound_on_every_run():
    phases: list = []

    def reconcile_fn():
        phases.append(actor_mod.current_principal())

    def scan_fn():
        phases.append(actor_mod.current_principal())

    daemon = _daemon(reconcile_fn, scan_fn)
    with actor_mod.bind_principal(HUMAN_ALICE):
        daemon.start()
        assert actor_mod.current_principal() is HUMAN_ALICE
        daemon.start()
        assert actor_mod.current_principal() is HUMAN_ALICE
        daemon.run_once()
        assert actor_mod.current_principal() is HUMAN_ALICE

    assert len(phases) == 3
    assert all(p is SYSTEM_SCHEDULER for p in phases)


def test_raising_phase_propagates_and_next_run_is_still_bound():
    def boom():
        raise RuntimeError("boom")

    daemon = _daemon(boom, lambda: None)
    with actor_mod.bind_principal(HUMAN_ALICE):
        with pytest.raises(RuntimeError, match="boom"):
            daemon.start()
        assert actor_mod.current_principal() is HUMAN_ALICE

    healthy: list = []

    def reconcile_fn():
        healthy.append(actor_mod.current_principal())

    healthy_daemon = _daemon(reconcile_fn, lambda: None)
    with actor_mod.bind_principal(HUMAN_ALICE):
        healthy_daemon.start()
        assert actor_mod.current_principal() is HUMAN_ALICE

    assert healthy == [SYSTEM_SCHEDULER]


def test_run_daemon_still_passes_advance_all_plans_as_reconcile_fn(
    tmp_path, monkeypatch
):
    """The composition root is untouched: no wrapper, no extra indirection."""
    instances: list = []

    class FakeDaemon:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            instances.append(self)

        def run_forever(self, stop_event) -> None:
            return None

    def fake_advance_all_plans():
        return None

    monkeypatch.setattr(daemon_mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "SchedulerDaemon", FakeDaemon)
    monkeypatch.setattr(daemon_mod, "build_bus", lambda: object())
    monkeypatch.setattr(daemon_mod, "scan_all_plans", lambda bus: None)
    monkeypatch.setattr(server_mod, "advance_all_plans", fake_advance_all_plans)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)

    assert daemon_mod.run_daemon() == 0

    assert len(instances) == 1
    assert instances[0].kwargs["reconcile_fn"] is fake_advance_all_plans
