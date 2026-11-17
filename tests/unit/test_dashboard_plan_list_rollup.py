"""The plan list is a roll-up across the namespaces the caller's view scope allows."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import dashboard_helpers as h
from pipeline import view_scope
from pipeline.store_memory import InMemoryRecordStore


class _Store:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records) -> None:
        self._records = records

    def records(self):
        return self._records

    def get_notification_records(self, plan_name, limit=5):
        """The facade method the summary's notification line reads; none here."""
        return []


class _Scope:
    """A view scope that answers a fixed set."""

    def __init__(self, answer) -> None:
        self._answer = answer

    def visible(self, principal):
        return self._answer


@pytest.fixture
def client() -> TestClient:
    return TestClient(d.app)


def _seed(monkeypatch, *plans):
    records = InMemoryRecordStore()
    for namespace, name in plans:
        records.put_manifest(
            namespace, name,
            {"stories": {"S1": {"summary": "x", "status": "todo"}},
             "repo_root": "/tmp/repo"},
        )
    monkeypatch.setattr(h, "_store", _Store(records))
    return records


def test_rollup_lists_every_visible_namespace_and_labels_each_plan(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope",
        lambda: _Scope(frozenset({"default", "a/b"})),
    )

    body = client.get("/api/plans").json()

    namespaces = {p["name"]: p.get("namespace") for p in body["plans"]}
    # A roll-up labels every row, including the request's own namespace.
    assert namespaces == {"alpha": "default", "beta": "a/b"}
    assert "degraded" not in body


def test_rollup_caps_the_namespace_count_and_says_so(client, monkeypatch):
    _seed(monkeypatch, ("default", "delta"), ("a/b", "alpha"), ("c/d", "gamma"))
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "1")
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope",
        lambda: _Scope(frozenset({"default", "a/b", "c/d"})),
    )

    body = client.get("/api/plans?include_archived=true").json()

    # Sorted: a/b first, so the single allowed namespace is a/b.
    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert body["degraded"] == ["namespace_cap"]


def test_an_unreadable_namespace_degrades_only_itself(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))

    class _Flaky(InMemoryRecordStore):
        def list_plans(self, namespace):
            if namespace == "a/b":
                raise RuntimeError("namespace offline")
            return super().list_plans(namespace)

    flaky = _Flaky()
    flaky.put_manifest("default", "alpha", {"stories": {"S1": {"status": "todo"}}})
    flaky.put_manifest("a/b", "beta", {"stories": {"S1": {"status": "todo"}}})
    monkeypatch.setattr(h, "_store", _Store(flaky))
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope",
        lambda: _Scope(frozenset({"default", "a/b"})),
    )

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert body["degraded"] == ["namespace_unavailable"]


def test_the_unscoped_view_reads_only_the_request_namespace(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"))
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope", lambda: _Scope(view_scope.ALL)
    )

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert body["plans"][0].get("namespace") is None
