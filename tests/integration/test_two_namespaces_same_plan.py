"""Two tenants, one plan name: the queue and pool advance each namespace's plan once (nq-06).

Plan names are unique only inside a namespace (ADR 0012), so ``demo`` held by
``acme/web`` and by ``beta/api`` is two plans. These tests drive the real
``AdvanceQueue``, the real ``sweep_namespaces`` and the real ``AdvanceWorkerPool``
threads; only the child process (``_spawn``) is a fake, and the store is a fake
record view. Nothing here reads the developer's store or starts a subprocess.
"""

from __future__ import annotations

import json
import threading

import pytest

import pipeline.server as server_mod
import pipeline.telemetry
from pipeline import advance_one, advance_sweep
from pipeline.advance_queue import AdvanceQueue
from pipeline.advance_workers import AdvanceWorkerPool
from pipeline.scheduler_scope import ONLY_VAR, SKIP_VAR

PLAN = "demo"
ACME = "acme/web"
BETA = "beta/api"
OTHER = "gamma/ops"
STALE_SECONDS = 60.0
NOW = 1000.0


def _active_manifest(namespace: str) -> dict:
    """A manifest with work left: one story still to do, so the sweep does not treat it as inert."""
    return {"name": PLAN, "namespace": namespace, "stories": {"s1": {"status": "todo"}}}


class FakeRecords:
    """A namespaced record view holding ``{namespace: {plan: manifest}}``."""

    def __init__(self, held: dict[str, dict[str, dict]]) -> None:
        self._held = held

    def list_namespaces(self) -> list[str]:
        return sorted(self._held)

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(self._held.get(namespace, {}))

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        return self._held.get(namespace, {}).get(plan_name)


class FakeStore:
    """The slice of ``server._store`` the scheduler reads: no default-namespace plans, one record view."""

    def __init__(self, records: FakeRecords) -> None:
        self._records = records

    def list_manifests(self) -> list[str]:
        return []

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        return None

    def records(self) -> FakeRecords:
        return self._records


class FakeChild:
    """Stands in for one worker child: answers every request with ``ok`` and records it."""

    def __init__(
        self,
        sent: list[tuple[str, str | None]],
        lock: threading.Lock,
        done: threading.Event,
        expected: int,
    ) -> None:
        self._sent = sent
        self._lock = lock
        self._done = done
        self._expected = expected

    def send(self, plan_name: str, namespace: str | None = None) -> None:
        with self._lock:
            self._sent.append((namespace, plan_name))
            if len(self._sent) >= self._expected:
                self._done.set()

    def read_line(self, timeout_s: float) -> str:
        return json.dumps({"ok": True})

    def poll(self) -> int | None:
        return None

    def terminate(self) -> int:
        return 0

    def close_input(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def two_tenants(monkeypatch: pytest.MonkeyPatch) -> FakeRecords:
    """Install a store where ``demo`` is held by ``acme/web`` and ``beta/api``, and no scope is set."""
    monkeypatch.delenv(ONLY_VAR, raising=False)
    monkeypatch.delenv(SKIP_VAR, raising=False)
    records = FakeRecords(
        {
            ACME: {PLAN: _active_manifest(ACME)},
            BETA: {PLAN: _active_manifest(BETA)},
        }
    )
    monkeypatch.setattr(server_mod, "_store", FakeStore(records))
    return records


def test_sweep_queues_demo_once_in_each_namespace(two_tenants: FakeRecords) -> None:
    queue = AdvanceQueue()
    pool = AdvanceWorkerPool(2, queue, plan_timeout_s=lambda: 5.0)

    result = advance_sweep.sweep_namespaces(queue, pool, STALE_SECONDS, NOW)

    assert result["namespaces"] == {ACME: [PLAN], BETA: [PLAN]}
    assert result["queued"] == []


def test_pool_advances_each_demo_once_with_its_own_namespace(
    two_tenants: FakeRecords, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = AdvanceQueue()
    pool = AdvanceWorkerPool(2, queue, plan_timeout_s=lambda: 5.0)
    sent: list[tuple[str, str | None]] = []
    lock = threading.Lock()
    done = threading.Event()
    monkeypatch.setattr(
        pool,
        "_spawn",
        lambda: FakeChild(sent, lock, done, expected=2),
    )
    advance_sweep.sweep_namespaces(queue, pool, STALE_SECONDS, NOW)

    pool.start()
    try:
        assert done.wait(timeout=10), "the pool did not advance both namespaces"
    finally:
        pool.stop()

    assert sorted(sent, key=lambda pair: pair[0]) == [(ACME, PLAN), (BETA, PLAN)]
    assert pool.stats()["advances"] == 2
    assert queue.last_advanced(PLAN, namespace=ACME) is not None
    assert queue.last_advanced(PLAN, namespace=BETA) is not None


def test_finishing_one_demo_does_not_mark_the_other_as_advanced(
    two_tenants: FakeRecords,
) -> None:
    queue = AdvanceQueue()
    pool = AdvanceWorkerPool(2, queue, plan_timeout_s=lambda: 5.0)
    advance_sweep.sweep_namespaces(queue, pool, STALE_SECONDS, NOW)

    first_namespace, _ = queue.take_entry(0.0)
    queue.finish(PLAN, namespace=first_namespace)

    other = BETA if first_namespace == ACME else ACME
    assert queue.last_advanced(PLAN, namespace=first_namespace) is not None
    assert queue.last_advanced(PLAN, namespace=other) is None


def test_request_for_demo_in_a_namespace_not_holding_it_is_refused_and_advances_nothing(
    two_tenants: FakeRecords, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    advanced: list[str] = []
    refused: list[str] = []
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(advance_one, "configure_child", lambda: None)
    monkeypatch.setattr(advance_one, "audit_refusal", refused.append)
    monkeypatch.setattr(pipeline.telemetry, "flush", lambda: None)
    monkeypatch.setattr(
        server_mod,
        "advance_pipeline",
        lambda plan: advanced.append(plan) or {"ok": True},
    )

    code = advance_one.main([PLAN, str(result_path), OTHER])

    assert code == 1
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "ok": False,
        "error": "namespace_mismatch",
    }
    assert refused == ["namespace_mismatch"]
    assert advanced == []
