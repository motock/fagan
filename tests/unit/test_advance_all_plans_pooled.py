"""TP-02: the scheduler's reconcile pass advances plans in a bounded pool.

``PipelineService.advance_all_plans`` used to call ``advance_pipeline`` for every
manifest in turn, so one reconcile pass cost the SUM of every plan's work. With
a few plans that exceeds the 900s reconcile deadline, the watchdog abandons the
pass, and the daemon restarts 300s later with the work still in flight.

The pool is deliberately narrow. Only the scheduler's own reconcile may use it,
recognised by the bound ``SYSTEM_SCHEDULER`` principal: the children re-bind
``system:scheduler``, so pooling a remote caller's ``advance_all_plans`` MCP
request would hand that caller the scheduler's privileges. Everything else
keeps the serial in-process loop.

These tests never spawn a real child: ``advance_pool.advance_plan_subprocess``
is the seam. The real-environment check lives in
``tests/integration/test_pooled_advance_real.py``.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

# pipeline.server must be imported before any other pipeline.* module: the
# package has a pre-existing import cycle, so a cold import of a leaf module
# first fails. Same idiom as tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server
from app.identity import Principal
from pipeline import actor, advance_pool
from pipeline.actor import SYSTEM_SCHEDULER
from pipeline.advance_pool import (
    _advance_workers,
    _plan_timeout_seconds,
    advance_all_plans_pooled,
    pooled_advance_enabled,
)
from pipeline.service import PipelineService

WORKERS_ENV = "PIPELINE_ADVANCE_WORKERS"
PLAN_TIMEOUT_ENV = "PIPELINE_ADVANCE_PLAN_TIMEOUT_SECONDS"

#: An OIDC-authenticated caller, not the deployment's own scheduler identity.
OIDC_PRINCIPAL = Principal(
    subject="oidc|alice@example.com",
    scopes=("pipeline:read", "pipeline:write"),
    auth_method="oidc",
)


class _FakeStore:
    """Minimal ``FileStore`` stand-in: the pool only needs the plan list."""

    def __init__(self, plans: list[str]) -> None:
        self._plans = list(plans)

    def list_manifests(self) -> list[str]:
        return list(self._plans)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Start every test from the documented defaults, whatever the shell says."""
    monkeypatch.delenv(WORKERS_ENV, raising=False)
    monkeypatch.delenv(PLAN_TIMEOUT_ENV, raising=False)


@pytest.fixture(autouse=True)
def _unbound_principal():
    """Stop one test's bound principal leaking into the next."""
    with actor.bind_principal(None):
        yield


@pytest.fixture(autouse=True)
def _fresh_rotation(monkeypatch: pytest.MonkeyPatch):
    """Reset the rotation cursor: it is module state shared across tests."""
    monkeypatch.setattr(advance_pool, "_rotation", 0)


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch):
    """Return a helper that makes the store list exactly the given plans."""

    def _set(names: list[str]) -> list[str]:
        monkeypatch.setattr(pipeline.server, "_store", _FakeStore(names))
        return names

    return _set


@pytest.fixture
def spawn_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Patch the child-process seam; the recorded (plan, timeout) pairs come back."""
    calls: list[tuple[str, float]] = []

    def _fake_spawn(plan_name: str, timeout_s: float) -> dict:
        calls.append((plan_name, timeout_s))
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(advance_pool, "advance_plan_subprocess", _fake_spawn)
    return calls


@pytest.fixture
def serial_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the serial in-process path and record every plan it advanced."""
    calls: list[str] = []

    def _fake_advance(plan_name: str) -> dict:
        calls.append(plan_name)
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", _fake_advance)
    return calls


def _warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.levelno == logging.WARNING]


# --------------------------------------------------------------------------
# Environment parsing
# --------------------------------------------------------------------------


class TestAdvanceWorkers:
    """``PIPELINE_ADVANCE_WORKERS`` parsing: default 1, cap 32, never crash."""

    def test_unset_is_one(self):
        assert _advance_workers() == 1

    def test_parses_a_valid_count(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "3")
        assert _advance_workers() == 3

    def test_malformed_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(WORKERS_ENV, "abc")
        caplog.set_level(logging.WARNING)
        assert _advance_workers() == 1
        assert len(_warnings(caplog.records)) == 1

    def test_fractional_is_malformed_not_rounded(self, monkeypatch, caplog):
        # int("3.5") raises ValueError; this knob is a count of processes, so
        # accepting "3.5" would silently pick one of two readings.
        monkeypatch.setenv(WORKERS_ENV, "3.5")
        caplog.set_level(logging.WARNING)
        assert _advance_workers() == 1
        assert len(_warnings(caplog.records)) == 1

    def test_zero_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(WORKERS_ENV, "0")
        caplog.set_level(logging.WARNING)
        assert _advance_workers() == 1
        assert len(_warnings(caplog.records)) == 1

    def test_negative_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(WORKERS_ENV, "-1")
        caplog.set_level(logging.WARNING)
        assert _advance_workers() == 1
        assert len(_warnings(caplog.records)) == 1

    def test_above_the_ceiling_is_capped(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "999")
        assert _advance_workers() == 32

    def test_ceiling_boundary(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "32")
        assert _advance_workers() == 32


class TestPlanTimeoutSeconds:
    """``PIPELINE_ADVANCE_PLAN_TIMEOUT_SECONDS`` parsing: default 600."""

    def test_unset_is_600(self):
        assert _plan_timeout_seconds() == 600.0

    def test_parses_a_valid_timeout(self, monkeypatch):
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "42.5")
        assert _plan_timeout_seconds() == 42.5

    def test_nan_warns_and_defaults(self, monkeypatch, caplog):
        # float("nan") <= 0 is False, so a bare ``value <= 0`` guard lets nan
        # through and ``min(nan, 600.0)`` is argument-order dependent.
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "nan")
        caplog.set_level(logging.WARNING)
        assert _plan_timeout_seconds() == 600.0
        assert len(_warnings(caplog.records)) == 1

    def test_inf_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "inf")
        caplog.set_level(logging.WARNING)
        assert _plan_timeout_seconds() == 600.0
        assert len(_warnings(caplog.records)) == 1

    def test_negative_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "-5")
        caplog.set_level(logging.WARNING)
        assert _plan_timeout_seconds() == 600.0
        assert len(_warnings(caplog.records)) == 1

    def test_zero_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "0")
        caplog.set_level(logging.WARNING)
        assert _plan_timeout_seconds() == 600.0
        assert len(_warnings(caplog.records)) == 1

    def test_malformed_warns_and_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "abc")
        caplog.set_level(logging.WARNING)
        assert _plan_timeout_seconds() == 600.0
        assert len(_warnings(caplog.records)) == 1


class TestPooledAdvanceEnabled:
    """Only ``system:scheduler`` with more than one worker may pool."""

    def test_scheduler_with_several_workers_pools(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "3")
        with actor.bind_principal(SYSTEM_SCHEDULER):
            assert pooled_advance_enabled() is True

    def test_nothing_bound_stays_serial(self, monkeypatch):
        # current_principal() falls back to LOCAL_OPERATOR; an MCP caller must
        # never get children running as system:scheduler.
        monkeypatch.setenv(WORKERS_ENV, "3")
        assert pooled_advance_enabled() is False

    def test_oidc_principal_stays_serial(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "3")
        with actor.bind_principal(OIDC_PRINCIPAL):
            assert pooled_advance_enabled() is False

    def test_single_worker_stays_serial(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "1")
        with actor.bind_principal(SYSTEM_SCHEDULER):
            assert pooled_advance_enabled() is False

    def test_other_system_principal_stays_serial(self, monkeypatch):
        monkeypatch.setenv(WORKERS_ENV, "3")
        other = Principal(subject="system:worker", auth_method="system")
        with actor.bind_principal(other):
            assert pooled_advance_enabled() is False


# --------------------------------------------------------------------------
# Entrypoint: which path does the reconcile pass take?
# --------------------------------------------------------------------------


class TestServiceEntrypoint:
    """The privilege boundary: only the scheduler's own reconcile may pool."""

    def test_scheduler_reconcile_takes_the_pool(
        self, monkeypatch, plans, spawn_calls, serial_calls, caplog
    ):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta", "gamma"])
        caplog.set_level(logging.INFO)

        with actor.bind_principal(SYSTEM_SCHEDULER):
            result = PipelineService().advance_all_plans()

        assert result["ok"] is True
        assert sorted(result["plans"]) == ["alpha", "beta", "gamma"]
        assert serial_calls == [], "the serial advance_pipeline must not run"
        assert sorted(name for name, _ in spawn_calls) == ["alpha", "beta", "gamma"]

        pass_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "plans=" in r.getMessage()
        ]
        assert len(pass_lines) == 1, pass_lines
        line = pass_lines[0]
        assert "plans=3" in line
        assert "workers=3" in line
        assert "elapsed_s=" in line
        assert "timed_out=0" in line

    def test_nothing_bound_takes_the_serial_loop(
        self, monkeypatch, plans, spawn_calls, serial_calls
    ):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta"])

        result = PipelineService().advance_all_plans()

        assert sorted(serial_calls) == ["alpha", "beta"]
        assert spawn_calls == [], "an unbound caller must never reach the children"
        assert result["plans"]["alpha"]["ok"] is True

    def test_oidc_caller_takes_the_serial_loop(
        self, monkeypatch, plans, spawn_calls, serial_calls
    ):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta"])

        with actor.bind_principal(OIDC_PRINCIPAL):
            PipelineService().advance_all_plans()

        assert sorted(serial_calls) == ["alpha", "beta"]
        assert spawn_calls == []

    def test_single_worker_takes_the_serial_loop(
        self, monkeypatch, plans, spawn_calls, serial_calls
    ):
        monkeypatch.setenv(WORKERS_ENV, "1")
        plans(["alpha", "beta"])

        with actor.bind_principal(SYSTEM_SCHEDULER):
            PipelineService().advance_all_plans()

        assert sorted(serial_calls) == ["alpha", "beta"]
        assert spawn_calls == []


# --------------------------------------------------------------------------
# Pool behaviour
# --------------------------------------------------------------------------


class TestPooledPass:
    """``advance_all_plans_pooled`` itself: concurrency, rotation, isolation."""

    def test_plans_run_concurrently(self, monkeypatch, plans):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta", "gamma"])

        def _slow_spawn(plan_name: str, timeout_s: float) -> dict:
            time.sleep(0.3)
            return {"ok": True}

        monkeypatch.setattr(advance_pool, "advance_plan_subprocess", _slow_spawn)

        started = time.monotonic()
        result = advance_all_plans_pooled()
        elapsed = time.monotonic() - started

        assert sorted(result["plans"]) == ["alpha", "beta", "gamma"]
        assert all(r["ok"] is True for r in result["plans"].values())
        # Serial would be ~0.9s; three concurrent 0.3s children must overlap.
        assert elapsed < 0.8, f"pass took {elapsed:.3f}s: the pool ran serially"

    def test_rotation_starts_with_a_different_plan_each_pass(
        self, monkeypatch, plans
    ):
        # One worker: a single thread executes in submission order, so the
        # recorder is deterministic (with 3 workers the order is thread-racy).
        monkeypatch.setenv(WORKERS_ENV, "1")
        plans(["A", "B", "C"])
        order: list[str] = []

        def _recording_spawn(plan_name: str, timeout_s: float) -> dict:
            order.append(plan_name)
            return {"ok": True}

        monkeypatch.setattr(advance_pool, "advance_plan_subprocess", _recording_spawn)

        advance_all_plans_pooled()
        first = list(order)
        order.clear()
        advance_all_plans_pooled()
        second = list(order)

        assert first == ["A", "B", "C"]
        assert second[0] == "B", f"call 2 started at {second[0]}, not B"
        assert second == ["B", "C", "A"]

    def test_expired_deadline_skips_every_plan(self, monkeypatch, plans, spawn_calls):
        # The fake clock is required: ``max(30, join - 60)`` floors the window
        # at 30s, so shortening the join timeout can never put the deadline in
        # the past. First monotonic() call sets the clock, every later call is
        # an hour on: deadline = 1000 + max(30, 900-60) = 1840, remaining -160.
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta", "gamma"])
        monkeypatch.setattr(
            advance_pool, "_reconcile_join_timeout_seconds", lambda: 900.0
        )
        readings = iter([1000.0])

        def _fake_monotonic() -> float:
            return next(readings, 2000.0)

        monkeypatch.setattr(advance_pool.time, "monotonic", _fake_monotonic)

        result = advance_all_plans_pooled()

        assert result["plans"] == {
            "alpha": {"ok": True, "skipped": "deadline"},
            "beta": {"ok": True, "skipped": "deadline"},
            "gamma": {"ok": True, "skipped": "deadline"},
        }
        assert spawn_calls == [], "a plan past the deadline must not spawn a child"

    def test_one_raising_plan_does_not_sink_the_others(
        self, monkeypatch, plans, caplog
    ):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans(["alpha", "beta", "gamma"])

        def _spawn(plan_name: str, timeout_s: float) -> dict:
            if plan_name == "beta":
                raise RuntimeError("boom /srv/secret")
            return {"ok": True}

        monkeypatch.setattr(advance_pool, "advance_plan_subprocess", _spawn)
        caplog.set_level(logging.DEBUG)

        result = advance_all_plans_pooled()

        assert sorted(result["plans"]) == ["alpha", "beta", "gamma"]
        assert result["plans"]["alpha"] == {"ok": True}
        assert result["plans"]["gamma"] == {"ok": True}
        assert result["plans"]["beta"] == {
            "ok": False,
            "error": "advance raised RuntimeError",
        }
        # The exception text carries payloads and paths: never echo it.
        dumped = json.dumps(result)
        assert "boom" not in dumped
        assert "/srv/secret" not in dumped

    def test_no_plans_returns_empty_without_a_pool(
        self, monkeypatch, plans, spawn_calls
    ):
        monkeypatch.setenv(WORKERS_ENV, "3")
        plans([])

        result = advance_all_plans_pooled()

        # min(workers, 0) would make ThreadPoolExecutor(max_workers=0) raise.
        assert result == {"ok": True, "plans": {}}
        assert spawn_calls == []

    def test_plan_timeout_is_clamped_to_the_remaining_window(
        self, monkeypatch, plans, spawn_calls
    ):
        monkeypatch.setenv(WORKERS_ENV, "1")
        monkeypatch.setenv(PLAN_TIMEOUT_ENV, "600")
        plans(["alpha"])
        # join timeout 900 -> window 840s, far longer than the plan timeout.
        monkeypatch.setattr(
            advance_pool, "_reconcile_join_timeout_seconds", lambda: 900.0
        )

        advance_all_plans_pooled()

        assert spawn_calls == [("alpha", 600.0)]
