"""Tests for the daemon-startup interpreter guard (P0-INTERP-03).

``run_daemon()`` must refuse to start when the process is running from a
deleted interpreter tree: it prints one ``scheduler_daemon: ERROR`` line to
stderr and returns 1, so launchd's ``KeepAlive`` restarts it on the current
interpreter. It must never reach ``run_forever`` in that case.

The report itself moved to :mod:`pipeline.config_provenance`; the last test
here pins that the moved function is callable and silent when the two config
layers agree. The exhaustive conflict matrix stays in
``test_scheduler_env_conflict_report.py``.
"""
from typing import ClassVar

import pytest

from pipeline import config_provenance, runtime_check
from pipeline import scheduler_daemon as mod
from pipeline import server as server_mod
from tests.unit._config_provenance_helpers import _write_json, _write_plist


class FakeSchedulerDaemon:
    """Stand-in for the real SchedulerDaemon that never loops for real."""

    instances: ClassVar[list["FakeSchedulerDaemon"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.run_forever_calls: list = []
        FakeSchedulerDaemon.instances.append(self)

    def run_forever(self, stop_event) -> None:
        self.run_forever_calls.append(stop_event)


@pytest.fixture
def run_daemon_env(tmp_path, monkeypatch):
    """Patch every collaborator run_daemon() wires up, plus PLAN_DIR.

    Mirrors the fixture in ``test_scheduler_daemon.py``. The env-conflict
    report is additionally stubbed out so these tests assert on the guard's
    stderr line alone, never on the operator's real plist / ~/.claude.json.
    """
    FakeSchedulerDaemon.instances = []
    monkeypatch.setattr(mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(mod, "SchedulerDaemon", FakeSchedulerDaemon)
    monkeypatch.setattr(mod, "_report_env_conflicts", lambda: None)

    sentinel_bus = object()
    build_bus_calls = []

    def fake_build_bus():
        build_bus_calls.append(True)
        return sentinel_bus

    monkeypatch.setattr(mod, "build_bus", fake_build_bus)

    scan_all_plans_calls = []

    def fake_scan_all_plans(bus):
        scan_all_plans_calls.append(bus)

    monkeypatch.setattr(mod, "scan_all_plans", fake_scan_all_plans)

    reconcile_calls = []

    def fake_advance_all_plans():
        reconcile_calls.append(True)

    monkeypatch.setattr(server_mod, "advance_all_plans", fake_advance_all_plans)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)

    return {
        "tmp_path": tmp_path,
        "sentinel_bus": sentinel_bus,
        "build_bus_calls": build_bus_calls,
        "scan_all_plans_calls": scan_all_plans_calls,
        "reconcile_calls": reconcile_calls,
        "fake_advance_all_plans": fake_advance_all_plans,
    }


# ---------------------------------------------------------------------------
# run_daemon() — interpreter guard
# ---------------------------------------------------------------------------


def test_run_daemon_returns_1_and_never_runs_when_interpreter_is_broken(
    run_daemon_env, monkeypatch, capsys
):
    problem = "Python interpreter base prefix is missing: /gone/3.14.9"
    monkeypatch.setattr(runtime_check, "interpreter_problem", lambda: problem)

    result = mod.run_daemon()

    assert result == 1
    assert FakeSchedulerDaemon.instances == []
    assert capsys.readouterr().err == f"scheduler_daemon: ERROR {problem}\n"


def test_run_daemon_proceeds_past_guard_when_interpreter_is_healthy(
    run_daemon_env, monkeypatch
):
    monkeypatch.setattr(runtime_check, "interpreter_problem", lambda: None)

    result = mod.run_daemon()

    assert result == 0


# ---------------------------------------------------------------------------
# config_provenance.report_env_conflicts — moved report
# ---------------------------------------------------------------------------


def test_report_env_conflicts_is_silent_when_both_layers_agree(
    tmp_path, monkeypatch, capsys
):
    plist = _write_plist(tmp_path, {"PIPELINE_MAX_CONCURRENT_AGENTS": "2"})
    claude = _write_json(
        tmp_path,
        {"mcpServers": {"pipeline": {"env": {"PIPELINE_MAX_CONCURRENT_AGENTS": "2"}}}},
    )
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(claude))

    config_provenance.report_env_conflicts(frozenset())

    assert capsys.readouterr().err == ""
