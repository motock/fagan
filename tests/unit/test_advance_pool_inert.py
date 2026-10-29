"""SQ-01: the advance passes skip plans that have no work for them.

An all-``done`` plan, a plan paused with no agent left to interrupt, and an
unapproved plan each cost a full child process (roughly a second of CPU to
import the pipeline) for nothing. ``pipeline.plan_activity.is_inert`` decides
who is skipped, and both the pooled pass and the serial loop consult it, so the
two paths skip exactly the same plans.

What must not change: every listed plan keeps its entry in the returned ``plans``
dict, a plan whose manifest cannot be read is advanced, and only the plans that
survive the filter are rotated and submitted.
"""

from __future__ import annotations

import pytest

# pipeline.server must be imported before any other pipeline.* module: the
# package has a pre-existing import cycle, so a cold import of a leaf module
# first fails. Same idiom as tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server
from pipeline import actor, advance_pool, plan_approval, service
from pipeline.actor import SYSTEM_SCHEDULER
from pipeline.service import PipelineService

WORKERS_ENV = "PIPELINE_ADVANCE_WORKERS"

_ALPHA = {"name": "alpha", "stories": {"a": {"status": "done"}}}
_BETA = {"name": "beta", "paused": True, "stories": {"b": {"status": "todo"}}}
_GAMMA = {"name": "gamma", "stories": {"c": {"status": "todo"}}}
_MANIFESTS = {"alpha": _ALPHA, "beta": _BETA, "gamma": _GAMMA}


class _FakeStore:
    """Minimal store stand-in: a plan list plus per-plan manifests."""

    def __init__(self, manifests: dict[str, dict | None]) -> None:
        self._manifests = dict(manifests)

    def list_manifests(self) -> list[str]:
        return list(self._manifests)

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        return self._manifests.get(plan_name)


@pytest.fixture(autouse=True)
def _approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the approval gate so these tests are about status and pause state."""
    monkeypatch.setattr(plan_approval, "is_activatable", lambda manifest: True)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the default (serial) worker count."""
    monkeypatch.delenv(WORKERS_ENV, raising=False)


@pytest.fixture(autouse=True)
def _fresh_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the rotation cursor: it is module state shared across tests."""
    monkeypatch.setattr(advance_pool, "_rotation", 0)


@pytest.fixture(autouse=True)
def _bound_scheduler():
    """Bind the scheduler principal for the pooled path, unbind afterwards."""
    with actor.bind_principal(SYSTEM_SCHEDULER):
        yield


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    """Install the fake store on both the pool's and the service's lookup."""
    fake = _FakeStore(_MANIFESTS)
    monkeypatch.setattr(pipeline.server, "_store", fake)
    monkeypatch.setattr(service, "_store", fake)
    return fake


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the child-process seam and record every plan submitted to it."""
    calls: list[str] = []

    def _fake_spawn(plan_name: str, timeout_s: float) -> dict:
        calls.append(plan_name)
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(advance_pool, "advance_plan_subprocess", _fake_spawn)
    return calls


@pytest.fixture
def advanced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the serial in-process advance and record every plan it advanced."""
    calls: list[str] = []

    def _fake_advance(plan_name: str) -> dict:
        calls.append(plan_name)
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(service, "advance_pipeline", _fake_advance)
    return calls


def test_pooled_pass_submits_only_the_active_plan(store, spawned) -> None:
    result = advance_pool.advance_all_plans_pooled()

    assert spawned == ["gamma"]
    assert set(result["plans"]) == {"alpha", "beta", "gamma"}
    assert result["plans"]["alpha"] == {"ok": True, "skipped": "inert"}
    assert result["plans"]["beta"] == {"ok": True, "skipped": "inert"}
    assert result["plans"]["gamma"].get("skipped") != "inert"


def test_pooled_pass_logs_only_submitted_plans(
    store, spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[tuple[int, int, float, int, int]] = []

    def _fake_log_pass(plans: int, workers: int, elapsed_s: float, skipped: int, timed_out: int) -> None:
        logged.append((plans, workers, elapsed_s, skipped, timed_out))

    monkeypatch.setattr(advance_pool, "_log_pass", _fake_log_pass)
    advance_pool.advance_all_plans_pooled()

    assert logged and logged[0][0] == 1


def test_pooled_pass_advances_every_plan_without_a_manifest(store, spawned) -> None:
    store._manifests["delta"] = None

    result = advance_pool.advance_all_plans_pooled()

    assert set(result["plans"]) == {"alpha", "beta", "gamma", "delta"}
    assert "delta" in spawned
    assert result["plans"]["delta"].get("skipped") != "inert"


def test_serial_pass_advances_only_the_active_plan(store, advanced) -> None:
    result = PipelineService().advance_all_plans()

    assert advanced == ["gamma"]
    assert set(result["plans"]) == {"alpha", "beta", "gamma"}
    assert result["plans"]["alpha"] == {"ok": True, "skipped": "inert"}
    assert result["plans"]["beta"] == {"ok": True, "skipped": "inert"}


def test_serial_pass_advances_a_plan_without_a_manifest(store, advanced) -> None:
    store._manifests["delta"] = None

    result = PipelineService().advance_all_plans()

    assert "delta" in advanced
    assert set(result["plans"]) == {"alpha", "beta", "gamma", "delta"}
