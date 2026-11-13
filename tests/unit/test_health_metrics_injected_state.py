"""The health and metrics readers take their state injected, not a plans path.

``health_extras`` and ``metrics_exposition.collect`` used to thread a
``plan_dir`` through their signatures. They now take the host state (the
scheduler snapshot and the host-local root) and the record store (the outbox
aggregate, the plan listing, the audit log) explicitly, defaulting to
``build_host_state()`` and ``build_store()``.

Graded through the public functions only:

* ``/metrics`` (``collect``) and ``/api/health`` (``health_extras``) read the
  same stored state and therefore agree on the scheduler and outbox fields;
* an injected empty store yields zero plan samples, and a manifest sitting in
  the host-local root is NOT counted, because the plan count comes from the
  store alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import health_freshness, metrics_exposition
from pipeline.audit_memory import InMemoryAuditLog
from pipeline.host_state import FileHostState
from pipeline.store_records import DEFAULT_NAMESPACE


class _Records:
    """The record view the readers use: plan listing, manifests, outbox aggregate."""

    def __init__(self, manifests=None, outbox=None) -> None:
        self._manifests = dict(manifests or {})
        self._outbox = outbox or {"files": 0, "records": 0, "oldest_age_s": None}

    def list_plans(self, namespace: str) -> list[str]:
        assert namespace == DEFAULT_NAMESPACE
        return sorted(self._manifests)

    def get_manifest(self, namespace: str, plan_name: str):
        assert namespace == DEFAULT_NAMESPACE
        return self._manifests.get(plan_name)

    def outbox_summary(self, namespace: str) -> dict:
        assert namespace == DEFAULT_NAMESPACE
        return dict(self._outbox)


class _Store:
    """An injected store: its record view and an in-memory audit log."""

    def __init__(self, records: _Records) -> None:
        self._records = records

    def records(self) -> _Records:
        return self._records

    def audit_log(self) -> InMemoryAuditLog:
        return InMemoryAuditLog()


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    monkeypatch.delenv("PIPELINE_STORE", raising=False)


def _write_fresh_scheduler_snapshot(root) -> None:
    now = datetime.now(UTC)
    payload = {"heartbeat_ts": (now - timedelta(seconds=5)).isoformat()}
    (root / health_freshness.HEALTH_FILENAME).write_text(json.dumps(payload))


def _approved(status: str) -> dict:
    return {"approval": {"status": status}}


def test_metrics_and_api_health_agree_on_the_same_stored_state(tmp_path):
    _write_fresh_scheduler_snapshot(tmp_path)
    host = FileHostState(root=tmp_path)
    store = _Store(
        _Records(
            manifests={"alpha": _approved("approved"), "beta": _approved("rejected")},
            outbox={"files": 2, "records": 7, "oldest_age_s": 30},
        )
    )

    api = health_freshness.health_extras(host, store)
    snapshot = metrics_exposition.collect(host, store)

    assert snapshot["fagan_scheduler_age_seconds"] == [({}, api["scheduler_age_s"])]
    assert snapshot["fagan_scheduler_healthy"] == [({}, bool(api["healthy"]))]
    assert snapshot["fagan_outbox_records"] == [({}, api["outbox"]["records"])]
    assert snapshot["fagan_outbox_oldest_age_seconds"] == [
        ({}, api["outbox"]["oldest_age_s"])
    ]


def test_an_injected_empty_store_yields_zero_plan_samples(tmp_path):
    host = FileHostState(root=tmp_path)
    store = _Store(_Records())

    snapshot = metrics_exposition.collect(host, store)

    assert [value for _, value in snapshot["fagan_plans"]] == [0, 0, 0, 0]


def test_plan_samples_come_from_the_store_not_the_host_root(tmp_path):
    (tmp_path / "gamma.manifest.json").write_text(json.dumps(_approved("approved")))
    host = FileHostState(root=tmp_path)

    snapshot = metrics_exposition.collect(host, _Store(_Records()))

    assert all(value == 0 for _, value in snapshot["fagan_plans"])


def test_plan_samples_count_the_store_plans_by_approval_status(tmp_path):
    host = FileHostState(root=tmp_path)
    store = _Store(
        _Records(
            manifests={
                "alpha": _approved("approved"),
                "beta": _approved("approved"),
                "gamma": _approved("pending_approval"),
            }
        )
    )

    snapshot = metrics_exposition.collect(host, store)

    counts = {labels["status"]: value for labels, value in snapshot["fagan_plans"]}
    assert counts == {
        "approved": 2,
        "pending_approval": 1,
        "rejected": 0,
        "none": 0,
    }
