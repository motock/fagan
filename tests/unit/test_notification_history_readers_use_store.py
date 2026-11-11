"""The notification-history READERS go through the record store.

``PipelineService.get_notification_sidecar_path`` used to hand the dashboard
route a ``<plan>.notifications.jsonl`` Path it composed itself, and
``plan_completion.notify_if_plan_completed`` composed the same Path itself and
parsed it with ``story_metrics.load_notification_records``.  Both readers now
read the plan's notification history from the record store:

* ``PipelineService.get_notification_history(plan_name)`` returns
  ``(records, malformed)`` and is the ONLY way a route gets the history;
* the dashboard's plan-metrics route obtains the history through that
  service method, not through a sidecar path;
* ``plan_completion.notify_if_plan_completed`` obtains the history the same
  way, so a plan whose notifications live in the store still gets them in
  its completion summary.

The old accessor is gone: ``get_notification_sidecar_path`` must not exist on
``PipelineService`` any more, so no route can compose the sidecar path.

The store is the seam: ``_store.records().get_notification_history(
DEFAULT_NAMESPACE, plan_name)``.  The tests below patch ``pipeline.server.
_store`` with a stand-in that serves records from memory, so the readers are
exercised against the record store rather than against the on-disk layout.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from pipeline import plan_completion
from pipeline import server as pipeline_server
from pipeline.service import PipelineService
from pipeline.store_records import DEFAULT_NAMESPACE, validate_plan_name

# ---------------------------------------------------------------------------
# Store doubles
# ---------------------------------------------------------------------------


class _Records:
    """The record view the readers reach through ``_store.records()``."""

    def __init__(self, owner, history=None):
        self._owner = owner
        self.history = {k: list(v) for k, v in (history or {}).items()}
        self.malformed = 0
        self.raise_on_read: Exception | None = None
        # The plan-completion marker and report are records of the store too.
        self.completed: set[tuple[str, str]] = set()
        self.reports: dict[tuple[str, str], str] = {}

    def is_plan_completed(self, namespace, plan_name):
        return (namespace, plan_name) in self.completed

    def mark_plan_completed(self, namespace, plan_name):
        key = (namespace, plan_name)
        if key in self.completed:
            return False
        self.completed.add(key)
        return True

    def put_plan_report(self, namespace, plan_name, text):
        self.reports[(namespace, plan_name)] = text

    def get_plan_report(self, namespace, plan_name):
        return self.reports.get((namespace, plan_name))

    def get_notification_history(self, namespace, plan_name):
        self._owner.calls.append((namespace, plan_name))
        if self.raise_on_read is not None:
            raise self.raise_on_read
        # The record store refuses a plan name it cannot key on (one holding
        # ".."), so the stand-in refuses it the same way.
        validate_plan_name(plan_name)
        return copy.deepcopy(self.history.get((namespace, plan_name), [])), self.malformed

    def get_journal(self, namespace, plan_name, story_key):
        """No journals in this stand-in: the plan summary does not need them."""
        return []


class _Store:
    """A stand-in for ``pipeline.server._store``.

    It serves the notification history both through ``records()`` (the record
    view) and directly, so the readers are free to reach for either; every
    read is recorded in ``calls`` either way.
    """

    def __init__(self, history=None, manifests=None):
        self.calls: list[tuple[str, str]] = []
        self._records = _Records(self, history)
        self._manifests = dict(manifests or {})

    def records(self):
        return self._records

    def get_notification_history(self, namespace, plan_name):
        return self._records.get_notification_history(namespace, plan_name)

    def get_manifest_or_none(self, plan_name):
        return self._manifests.get(plan_name)


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def patched_store(monkeypatch, store, tmp_path):
    """Patch the canonical store binding the service resolves at call time."""
    monkeypatch.setattr(pipeline_server, "_store", store)
    # The plans directory is empty and isolated: a reader that still went to
    # the on-disk sidecar would find nothing there, so a test that serves
    # history can only be green when the store really served it.
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    return store


@pytest.fixture
def client():
    return TestClient(dashboard.app)


def _record(story_key, event, ts="2026-01-01T00:00:00Z", dedup_key="d"):
    return {
        "ts": ts,
        "story_key": story_key,
        "event": event,
        "dedup_key": dedup_key,
    }


# ---------------------------------------------------------------------------
# PipelineService.get_notification_history
# ---------------------------------------------------------------------------


def test_service_exposes_get_notification_history():
    """The service exposes the new history accessor."""
    assert callable(PipelineService.get_notification_history)


def test_service_get_notification_history_returns_records_and_malformed(
    patched_store,
):
    """The service returns ``(records, malformed)`` for the plan."""
    patched_store._records.history[(DEFAULT_NAMESPACE, "alpha")] = [
        {"ts": "t1", "message": "hello"},
        {"ts": "t2", "message": None},
    ]

    records, malformed = dashboard._service.get_notification_history("alpha")

    assert records == [
        {"ts": "t1", "message": "hello"},
        {"ts": "t2", "message": None},
    ]
    assert malformed == 0


def test_service_get_notification_history_reads_the_default_namespace(
    patched_store,
):
    """The service reads the DEFAULT namespace of the plan's history."""
    patched_store._records.history[(DEFAULT_NAMESPACE, "alpha")] = [
        {"ts": "t1", "message": "hello"}
    ]

    dashboard._service.get_notification_history("alpha")

    assert (DEFAULT_NAMESPACE, "alpha") in patched_store.calls
    assert {namespace for namespace, _ in patched_store.calls} == {DEFAULT_NAMESPACE}


def test_service_get_notification_history_plan_without_history_is_empty(
    patched_store,
):
    """Boundary: a plan with no notifications yields ``([], 0)``."""
    records, malformed = dashboard._service.get_notification_history("ghost")

    assert records == []
    assert malformed == 0


def test_service_get_notification_history_invalid_plan_name_is_refused(
    patched_store,
):
    """NEGATIVE: an invalid plan name is refused and nothing is read."""
    with pytest.raises(ValueError):
        dashboard._service.get_notification_history("../escape")

    assert patched_store.calls == []


def test_service_no_longer_exposes_the_sidecar_path_accessor():
    """REGRESSION: the sidecar-path accessor is gone from the service.

    Nothing may compose ``<plan>.notifications.jsonl`` any more: the history
    is read from the store, so a path accessor would only invite a second
    reader to bypass the store.
    """
    assert not hasattr(PipelineService, "get_notification_sidecar_path")


def test_dashboard_source_has_no_sidecar_path_accessor():
    """REGRESSION: no route composes the notifications sidecar path.

    Checked on the dashboard module (any attribute or call) rather than on
    one function, so a new route cannot reintroduce it.
    """
    source = inspect.getsource(dashboard)
    assert "get_notification_sidecar_path" not in source
    assert "notifications.jsonl" not in source


# ---------------------------------------------------------------------------
# The dashboard plan-metrics route reads through the store
# ---------------------------------------------------------------------------


def test_metrics_route_serves_history_from_the_store(patched_store, client):
    """The route's body comes from the store's history, not from a file."""
    patched_store._manifests["alpha"] = {"stories": {}}
    patched_store._records.history[(DEFAULT_NAMESPACE, "alpha")] = [
        _record("S1", "dispatch_failed", dedup_key="d1"),
        _record("S1", "tests_failed", ts="2026-01-01T00:00:01Z", dedup_key="d2"),
    ]

    response = client.get("/api/plans/alpha/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"] == "alpha"
    assert len(body["stories"]) == 1
    story = body["stories"][0]
    assert story["story_key"] == "S1"
    assert story["dispatch_failures"] == 1
    assert story["rework_cycles"] == 1
    assert body["rollup"]["stories_total"] == 1
    assert body["malformed_lines"] == 0
    assert (DEFAULT_NAMESPACE, "alpha") in patched_store.calls
    assert {namespace for namespace, _ in patched_store.calls} == {DEFAULT_NAMESPACE}


def test_metrics_route_matches_the_same_files_served_by_the_file_store(
    tmp_path, monkeypatch, client
):
    """The body is the same whether the history is in files or in the store.

    The file store reads ``<plan>.notifications.jsonl`` under the canonical
    PLAN_DIR binding; the in-memory store serves the same records.  Both must
    produce the same response body.
    """
    records = [
        _record("S1", "dispatch_failed", dedup_key="d1"),
        _record("S2", "story_merged", ts="2026-01-01T00:00:01Z", dedup_key="d2"),
    ]
    (tmp_path / "alpha.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": []})
    )
    (tmp_path / "alpha.notifications.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)

    from_files = client.get("/api/plans/alpha/metrics")
    assert from_files.status_code == 200, from_files.text

    memory = _Store()
    memory._manifests["alpha"] = {"stories": {}}
    memory._records.history[(DEFAULT_NAMESPACE, "alpha")] = records
    monkeypatch.setattr(pipeline_server, "_store", memory)

    from_store = client.get("/api/plans/alpha/metrics")
    assert from_store.status_code == 200, from_store.text

    assert from_store.json() == from_files.json()


def test_metrics_route_plan_without_history_is_the_empty_rollup(
    patched_store, client
):
    """Boundary: a plan with no notification history yields the empty rollup."""
    patched_store._manifests["alpha"] = {"stories": {}}

    response = client.get("/api/plans/alpha/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stories"] == []
    assert body["malformed_lines"] == 0
    assert body["rollup"]["stories_total"] == 0
    assert body["rollup"]["stories_merged"] == 0


def test_metrics_route_reports_the_stores_malformed_count(patched_store, client):
    """The malformed count the store reports reaches the response unchanged."""
    patched_store._manifests["alpha"] = {"stories": {}}
    patched_store._records.malformed = 3

    response = client.get("/api/plans/alpha/metrics")

    assert response.status_code == 200, response.text
    assert response.json()["malformed_lines"] == 3


def test_metrics_route_malformed_count_from_a_corrupt_file_reaches_the_response(
    tmp_path, monkeypatch, client
):
    """A corrupt JSONL line is counted, not raised, and the count is served.

    This is the file-store path: the sidecar holds one good line and one
    unparseable line.
    """
    (tmp_path / "alpha.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": []})
    )
    (tmp_path / "alpha.notifications.jsonl").write_text(
        json.dumps(_record("S1", "dispatch_failed")) + "\n{not valid json\n"
    )
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)

    response = client.get("/api/plans/alpha/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["malformed_lines"] == 1
    assert len(body["stories"]) == 1
    assert body["stories"][0]["story_key"] == "S1"


def test_metrics_route_invalid_plan_name_is_refused_without_reading_the_store(
    patched_store,
):
    """NEGATIVE: an invalid plan name is refused and reads nothing.

    ``a b`` is outside ``[A-Za-z0-9._-]``, so the key validation refuses it
    before the store is touched: the request must not be answered with a
    metrics body, and the store must not be read at all.
    """
    lenient = TestClient(dashboard.app, raise_server_exceptions=False)
    response = lenient.get("/api/plans/a b/metrics")

    assert response.status_code >= 400, response.text
    assert patched_store.calls == []


def test_metrics_route_non_routable_plan_name_is_the_existing_4xx(
    patched_store, client
):
    """NEGATIVE: a plan name that is not routable is the existing 4xx.

    A plan name carrying a path separator never reaches the route at all, so
    the answer is the router's own 404 and the store is never read.
    """
    response = client.get("/api/plans/bad%2Fname/metrics")

    assert response.status_code == 404, response.text
    assert patched_store.calls == []


def test_metrics_route_unknown_plan_is_404(patched_store, client):
    """NEGATIVE: an unknown plan is a 404, exactly as before."""
    response = client.get("/api/plans/ghost/metrics")

    assert response.status_code == 404
    assert response.json()["detail"] == "No manifest for plan 'ghost'"


# ---------------------------------------------------------------------------
# plan_completion reads the history from the store too
# ---------------------------------------------------------------------------


class _PlanHarness:
    """Stubs for ``notify_if_plan_completed``."""

    def __init__(self, tmp_path, monkeypatch, store):
        self.tmp_path = tmp_path
        self.store = store
        self.notify_calls = []
        self.summary_records = []

        monkeypatch.setattr(plan_completion, "PLAN_DIR", tmp_path)
        # The plan-completion module builds its own store; point that lookup
        # at the same records the service would read, so the history comes
        # from one place whichever seam the reader reaches for.
        monkeypatch.setattr(
            plan_completion, "build_store", lambda *a, **k: store
        )
        monkeypatch.setattr(pipeline_server, "_store", store)

        def fake_notify(plan_name, message, **kwargs):
            self.notify_calls.append((plan_name, message, kwargs))

        def fake_summary(plan_name, manifest, records):
            self.summary_records.append(records)
            return f"SUMMARY[{plan_name}]"

        monkeypatch.setattr(plan_completion, "_notify_user", fake_notify)
        monkeypatch.setattr(plan_completion, "format_plan_summary", fake_summary)

    def marker_path(self, plan_name):
        return self.tmp_path / f"{plan_name}.plan_completed"

    def call(self, plan_name, manifest):
        return plan_completion.notify_if_plan_completed(plan_name, manifest)


@pytest.fixture
def plan_harness(tmp_path, monkeypatch):
    return _PlanHarness(tmp_path, monkeypatch, _Store())


def _done_manifest():
    return {"title": "t", "stories": {"s1": {"status": "done"}}}


def test_plan_completion_summary_includes_the_stored_history(plan_harness):
    """The completion summary is rendered from the STORE's history."""
    plan_harness.store._records.history[(DEFAULT_NAMESPACE, "plana")] = [
        {"ts": "t1", "message": "hello"},
        {"ts": "t2", "message": "world"},
    ]

    result = plan_harness.call("plana", _done_manifest())

    assert result is True
    assert len(plan_harness.summary_records) == 1
    assert list(plan_harness.summary_records[0]) == [
        {"ts": "t1", "message": "hello"},
        {"ts": "t2", "message": "world"},
    ]
    assert (DEFAULT_NAMESPACE, "plana") in plan_harness.store.calls
    assert {namespace for namespace, _ in plan_harness.store.calls} == {
        DEFAULT_NAMESPACE
    }


def test_plan_completion_without_history_still_completes(plan_harness):
    """Boundary: a plan with no notifications still completes."""
    result = plan_harness.call("planb", _done_manifest())

    assert result is True
    assert len(plan_harness.summary_records) == 1
    assert list(plan_harness.summary_records[0]) == []


def test_plan_completion_history_read_failure_never_raises(plan_harness, caplog):
    """NEGATIVE: a raising history read is logged, not raised.

    The function runs inside a scheduler tick: it must never break the
    story transition that triggered it, and a failed read must not mark the
    plan as notified (so a later tick can retry).
    """
    plan_harness.store._records.raise_on_read = RuntimeError("store down")

    with caplog.at_level(logging.WARNING):
        result = plan_harness.call("planc", _done_manifest())

    assert result is False
    assert plan_harness.notify_calls == []
    assert not plan_harness.marker_path("planc").exists(), (
        "a failed history read must not write the once-only marker"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a failed history read must be logged"
    )
