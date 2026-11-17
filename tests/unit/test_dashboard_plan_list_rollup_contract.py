"""Extra contract for the plan-list roll-up: helper placement, the cap, filters.

The roll-up story moves the plan-list body out of ``app/dashboard.py`` into
``app/dashboard_helpers.py`` so the dashboard module stays inside its line
budget, and makes the list read every namespace the caller's view scope
allows. These tests grade the parts of that contract the headline roll-up
tests do not cover: where the shared helpers live, how the namespace cap
reads its limit, how the ``repo`` filter and the archived-plan filter behave
across namespaces, and that a roll-up never reads another partition's
notifications.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import dashboard_helpers as h
from pipeline import view_scope
from pipeline.store_memory import InMemoryRecordStore
from tests.unit._dashboard_helpers import plan_dir  # noqa: F401


class _Store:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records) -> None:
        self._records = records
        self.notification_reads: list[str] = []

    def records(self):
        return self._records

    def get_notification_records(self, plan_name, limit=5):
        """Record the read so a test can prove a foreign partition is not read."""
        self.notification_reads.append(plan_name)
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


def _seed(monkeypatch, *plans, repo_root="/tmp/repo"):
    """Seed manifests in an in-memory record store and stub the store seam."""
    records = InMemoryRecordStore()
    for namespace, name in plans:
        records.put_manifest(
            namespace, name,
            {"stories": {"S1": {"summary": "x", "status": "todo"}},
             "repo_root": repo_root},
        )
    store = _Store(records)
    monkeypatch.setattr(h, "_store", store)
    return store


def _scope(monkeypatch, namespaces):
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope",
        lambda: _Scope(frozenset(namespaces)),
    )


# --- the shared helpers live in the helpers module -------------------------


def test_the_plan_list_helper_lives_in_the_helpers_module():
    assert callable(h.plan_list)


def test_the_dashboard_imports_the_shared_plan_list_helper():
    assert d.plan_list is h.plan_list


def test_the_dashboard_imports_the_shared_archived_plan_reader():
    assert d._read_archived_plans is h._read_archived_plans


def test_the_repo_path_normalizer_lives_in_the_helpers_module():
    assert callable(h._normalize_repo_path)


# --- the namespace cap ----------------------------------------------------


def test_the_cap_defaults_to_twenty_namespaces():
    assert h.PLAN_LIST_NAMESPACE_CAP_DEFAULT == 20


def test_the_cap_reads_its_limit_from_the_documented_env_var():
    assert h.PLAN_LIST_NAMESPACE_CAP_ENV == "PIPELINE_PLAN_LIST_NAMESPACE_CAP"


def test_a_view_at_the_cap_is_not_degraded(client, monkeypatch):
    names = {f"ns{i:02d}" for i in range(20)}
    _seed(monkeypatch, *[(n, f"plan-{n}") for n in names])
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "20")
    _scope(monkeypatch, names)

    body = client.get("/api/plans").json()

    assert len(body["plans"]) == 20
    assert "degraded" not in body


def test_a_view_over_the_cap_is_truncated_and_degraded(client, monkeypatch):
    names = {f"ns{i:02d}" for i in range(21)}
    _seed(monkeypatch, *[(n, f"plan-{n}") for n in names])
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "20")
    _scope(monkeypatch, names)

    body = client.get("/api/plans").json()

    assert len(body["plans"]) == 20
    assert body["degraded"] == ["namespace_cap"]


def test_an_unusable_cap_falls_back_to_the_default(client, monkeypatch):
    names = {f"ns{i:02d}" for i in range(21)}
    _seed(monkeypatch, *[(n, f"plan-{n}") for n in names])
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "not-a-number")
    _scope(monkeypatch, names)

    body = client.get("/api/plans").json()

    # The default cap still applies: 21 visible namespaces is over 20.
    assert len(body["plans"]) == 20
    assert body["degraded"] == ["namespace_cap"]


def test_a_zero_cap_falls_back_to_the_default(client, monkeypatch):
    names = {f"ns{i:02d}" for i in range(21)}
    _seed(monkeypatch, *[(n, f"plan-{n}") for n in names])
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "0")
    _scope(monkeypatch, names)

    body = client.get("/api/plans").json()

    assert len(body["plans"]) == 20
    assert body["degraded"] == ["namespace_cap"]


def test_an_empty_view_scope_cap_reads_only_the_request_namespace(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"))
    monkeypatch.setenv("PIPELINE_PLAN_LIST_NAMESPACE_CAP", "")
    _scope(monkeypatch, {"default"})

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert "degraded" not in body


# --- a roll-up never reads another partition's notifications ----------------


def test_a_rollup_does_not_read_another_partitions_notifications(client, monkeypatch):
    store = _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans").json()

    assert {p["name"] for p in body["plans"]} == {"alpha", "beta"}
    # Only the request's own partition is read for notifications.
    assert store.notification_reads == ["alpha"]


def test_a_foreign_partition_summary_carries_no_notification_line(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans").json()

    foreign = [p for p in body["plans"] if p["namespace"] == "a/b"]
    assert foreign and all("latest_notification" not in p for p in foreign)


# --- the repo filter applies across the roll-up ----------------------------


def test_the_repo_filter_applies_to_every_visible_namespace(client, monkeypatch):
    _seed(
        monkeypatch,
        ("default", "alpha"), ("a/b", "beta"),
        repo_root="/tmp/repo",
    )
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans", params={"repo": "/tmp/other"}).json()

    assert body["plans"] == []


def test_the_repo_filter_keeps_the_matching_plans_of_every_namespace(client, monkeypatch):
    _seed(
        monkeypatch,
        ("default", "alpha"), ("a/b", "beta"),
        repo_root="/tmp/repo",
    )
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans", params={"repo": "/tmp/repo"}).json()

    assert {p["name"] for p in body["plans"]} == {"alpha", "beta"}


def test_the_repo_filter_normalizes_the_manifest_root(client, monkeypatch, tmp_path):
    _seed(monkeypatch, ("default", "alpha"), repo_root=str(tmp_path) + "/./repo")
    _scope(monkeypatch, {"default"})

    body = client.get("/api/plans", params={"repo": str(tmp_path) + "/repo"}).json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]


def test_repo_all_does_not_filter_the_rollup(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"), repo_root="/tmp/repo")
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans", params={"repo": "all"}).json()

    assert {p["name"] for p in body["plans"]} == {"alpha", "beta"}


# --- the archived-plan filter applies across the roll-up -------------------


def test_an_archived_plan_is_hidden_from_the_rollup(client, monkeypatch, plan_dir):
    (plan_dir / ".dashboard_ui_state.json").write_text('{"archived": ["beta"]}')
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]


def test_include_archived_shows_the_archived_plan_of_another_namespace(
    client, monkeypatch, plan_dir,
):
    (plan_dir / ".dashboard_ui_state.json").write_text('{"archived": ["beta"]}')
    _seed(monkeypatch, ("default", "alpha"), ("a/b", "beta"))
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans", params={"include_archived": "true"}).json()

    assert {p["name"] for p in body["plans"]} == {"alpha", "beta"}


# --- the request's own namespace keeps the pre-roll-up shape --------------


def test_a_scope_of_only_the_request_namespace_keeps_the_plain_shape(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"))
    _scope(monkeypatch, {"default"})

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert "namespace" not in body["plans"][0]


def test_the_unscoped_view_keeps_the_plain_shape(client, monkeypatch):
    _seed(monkeypatch, ("default", "alpha"))
    monkeypatch.setattr(
        h.pipeline_view_scope, "active_view_scope", lambda: _Scope(view_scope.ALL)
    )

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["alpha"]
    assert "namespace" not in body["plans"][0]


# --- the roll-up is ordered newest first -----------------------------------


def test_the_rollup_is_ordered_newest_first(client, monkeypatch):
    records = InMemoryRecordStore()
    records.put_manifest("default", "older", {"stories": {}})
    records.put_manifest("a/b", "newer", {"stories": {}})
    store = _Store(records)
    times = {("default", "older"): 1.0, ("a/b", "newer"): 2.0}
    store._records = _TimedRecords(records, times)
    monkeypatch.setattr(h, "_store", store)
    _scope(monkeypatch, {"default", "a/b"})

    body = client.get("/api/plans").json()

    assert [p["name"] for p in body["plans"]] == ["newer", "older"]


class _TimedRecords:
    """A record view whose manifest timestamps are fixed by the test."""

    def __init__(self, records, times) -> None:
        self._records = records
        self._times = times

    def __getattr__(self, attr):
        return getattr(self._records, attr)

    def get_manifest_updated_at(self, namespace, plan_name):
        return self._times.get((namespace, plan_name))
