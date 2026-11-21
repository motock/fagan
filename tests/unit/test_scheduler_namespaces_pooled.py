"""nq-05: non-default namespaces advance through the worker pool in the default mode.

When ``advance_pool.pooled_advance_enabled()`` is true, each plan in a
non-default namespace is advanced in a child process bound to that namespace.
When it is false, the in-process path runs exactly as before. The child entry
point refuses an invalid namespace argument and advances nothing. Each pass
starts its namespace walk at a different namespace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.server
import pipeline.telemetry
from pipeline import advance_one, advance_pool, scheduler_namespaces
from pipeline.actor import current_namespace


class _Records:
    """A namespaced record view: plan lists and manifests keyed by namespace."""

    def __init__(
        self,
        plans: dict[str, list[str]],
        manifests: dict[tuple[str, str], dict] | None = None,
    ) -> None:
        self._plans = plans
        self._manifests = manifests or {}

    def list_plans(self, namespace: str) -> list[str]:
        return list(self._plans.get(namespace, []))

    def get_manifest(self, namespace: str, plan_name: str) -> dict:
        return self._manifests.get((namespace, plan_name), {})


def _server(calls: list[tuple[str, str]]) -> SimpleNamespace:
    """Stand-in for ``pipeline.server``: records each in-process advance and its namespace."""

    def advance_pipeline(plan_name: str) -> dict:
        calls.append((current_namespace(), plan_name))
        return {"ok": True, "plan": plan_name}

    return SimpleNamespace(advance_pipeline=advance_pipeline)


@pytest.fixture
def pooled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str]]:
    """Pooling on; record each child spawn as ``(namespace, plan_name)``."""
    spawned: list[tuple[str | None, str]] = []

    def fake_spawn(plan_name: str, timeout_s: float, *, namespace: str | None = None) -> dict:
        spawned.append((namespace, plan_name))
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(advance_pool, "pooled_advance_enabled", lambda: True)
    monkeypatch.setattr(advance_pool, "advance_plan_subprocess", fake_spawn)
    return spawned


def test_pooling_on_sends_each_plan_through_a_child_with_its_namespace(
    pooled: list[tuple[str | None, str]],
) -> None:
    calls: list[tuple[str, str]] = []
    records = _Records({"beta": ["p1", "p2"]})

    result = scheduler_namespaces._advance_others(_server(calls), records, ["beta"])

    assert pooled == [("beta", "p1"), ("beta", "p2")]
    assert calls == []
    assert result["beta"] == {
        "ok": True,
        "plans": {"p1": {"ok": True, "plan": "p1"}, "p2": {"ok": True, "plan": "p2"}},
    }


def test_pooling_off_advances_in_process_under_the_namespace_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(advance_pool, "pooled_advance_enabled", lambda: False)
    monkeypatch.setattr(
        advance_pool,
        "advance_plan_subprocess",
        lambda plan_name, timeout_s, **_: spawned.append(plan_name) or {"ok": True},
    )
    calls: list[tuple[str, str]] = []
    records = _Records({"beta": ["p1", "p2"]})

    result = scheduler_namespaces._advance_others(_server(calls), records, ["beta"])

    assert spawned == []
    assert calls == [("beta", "p1"), ("beta", "p2")]
    assert result["beta"]["plans"]["p1"] == {"ok": True, "plan": "p1"}


def test_child_timeout_is_reported_for_its_plan_and_the_next_plan_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timed_out = {"ok": False, "error": "advance timed out", "timed_out": True}

    def fake_spawn(plan_name: str, timeout_s: float, *, namespace: str | None = None) -> dict:
        return dict(timed_out) if plan_name == "p1" else {"ok": True, "plan": plan_name}

    monkeypatch.setattr(advance_pool, "pooled_advance_enabled", lambda: True)
    monkeypatch.setattr(advance_pool, "advance_plan_subprocess", fake_spawn)
    records = _Records({"beta": ["p1", "p2"]})

    result = scheduler_namespaces._advance_others(_server([]), records, ["beta"])

    assert result["beta"]["plans"]["p1"] == timed_out
    assert result["beta"]["plans"]["p2"] == {"ok": True, "plan": "p2"}


def test_manifest_naming_another_namespace_is_refused_before_any_child_starts(
    monkeypatch: pytest.MonkeyPatch, pooled: list[tuple[str | None, str]]
) -> None:
    refused: list[str] = []
    monkeypatch.setattr(scheduler_namespaces, "audit_refusal", refused.append)
    records = _Records({"beta": ["p1"]}, {("beta", "p1"): {"namespace": "alpha"}})

    result = scheduler_namespaces._advance_others(_server([]), records, ["beta"])

    assert pooled == []
    assert result["beta"]["plans"]["p1"] == {"ok": False, "error": "namespace_mismatch"}
    assert refused == ["namespace_mismatch"]


def test_child_entry_point_refuses_an_invalid_namespace_and_advances_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused: list[str] = []
    advanced: list[str] = []
    monkeypatch.setattr(advance_one, "configure_child", lambda: None)
    monkeypatch.setattr(advance_one, "audit_refusal", refused.append)
    monkeypatch.setattr(pipeline.telemetry, "flush", lambda: None)
    monkeypatch.setattr(
        pipeline.server, "advance_pipeline", lambda plan_name: advanced.append(plan_name) or {"ok": True}
    )
    result_path = tmp_path / "result.json"

    code = advance_one.main(["demo-plan", str(result_path), "Not A Namespace"])

    assert code == 1
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "ok": False,
        "error": "namespace_malformed",
    }
    assert advanced == []
    assert refused == ["namespace_malformed"]


def test_child_argv_carries_the_namespace_only_when_one_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    def fake_argv(plan_name: str, result_path: str, *namespace: str) -> list[str]:
        seen.append((plan_name, namespace))
        return [sys.executable, "-c", "import sys\nsys.exit(0)\n"]

    monkeypatch.setattr(advance_pool, "_child_argv", fake_argv)

    advance_pool.advance_plan_subprocess("demo-plan", 30.0)
    advance_pool.advance_plan_subprocess("demo-plan", 30.0, namespace="beta")

    assert seen == [("demo-plan", ()), ("demo-plan", ("beta",))]


def test_two_consecutive_passes_start_at_different_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(advance_pool, "pooled_advance_enabled", lambda: False)
    monkeypatch.setattr(scheduler_namespaces, "_namespace_rotation", 0, raising=False)
    records = _Records({"alpha": ["p"], "beta": ["p"], "gamma": ["p"]})
    namespaces = ["alpha", "beta", "gamma"]

    first = next(iter(scheduler_namespaces._advance_others(_server([]), records, namespaces)))
    second = next(iter(scheduler_namespaces._advance_others(_server([]), records, namespaces)))

    assert first != second
