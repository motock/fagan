"""Plan-list freshness comes from the record store, not a manifest stat.

``app/dashboard_helpers.py::_plan_summary`` reports ``updated_at`` as the
record store's ``get_manifest_updated_at`` for the ``default`` namespace.
A plan whose manifest has vanished reports ``None``, which must map to
``0.0`` so it sorts last and the list still renders.

The record store is stubbed only at the ``dashboard_helpers._store``
boundary; the route tests otherwise run against the real file store.
"""
import json
import os

from app import dashboard_helpers
from app.dashboard_helpers import _plan_summary
from tests.unit._dashboard_helpers import client, plan_dir  # noqa: F401


class _FakeRecords:
    def __init__(self, timestamps):
        self._timestamps = timestamps
        self.calls = []

    def get_manifest_updated_at(self, namespace, plan_name):
        self.calls.append((namespace, plan_name))
        return self._timestamps.get(plan_name)


class _FakeStore:
    def __init__(self, timestamps):
        self.records_view = _FakeRecords(timestamps)

    def records(self):
        return self.records_view


def _manifest():
    return {"epics": {}, "stories": {}}


def test_plan_summary_reports_record_timestamp(monkeypatch):
    store = _FakeStore({"plan-a": 1234.5})
    monkeypatch.setattr(dashboard_helpers, "_store", store)

    summary = _plan_summary("plan-a", _manifest())

    assert summary["updated_at"] == 1234.5
    assert store.records_view.calls == [("default", "plan-a")]


def test_plan_summary_vanished_manifest_reports_zero(monkeypatch):
    store = _FakeStore({})
    monkeypatch.setattr(dashboard_helpers, "_store", store)

    summary = _plan_summary("plan-gone", _manifest())

    assert summary["updated_at"] == 0.0


def test_plan_summary_vanished_manifest_does_not_raise(monkeypatch):
    store = _FakeStore({})
    monkeypatch.setattr(dashboard_helpers, "_store", store)

    summary = _plan_summary("plan-gone", _manifest())

    assert summary["name"] == "plan-gone"


def test_plan_list_sorts_newest_record_first(client, plan_dir):
    for name, mtime in (("older", 1_000_000), ("newest", 3_000_000), ("middle", 2_000_000)):
        path = plan_dir / f"{name}.manifest.json"
        path.write_text(json.dumps({"epics": {}, "stories": {}}))
        os.utime(path, (mtime, mtime))

    res = client.get("/api/plans")

    assert res.status_code == 200
    assert [p["name"] for p in res.json()["plans"]] == ["newest", "middle", "older"]


def test_plan_list_with_vanished_manifest_sorts_last_and_does_not_raise(
    client, plan_dir, monkeypatch,
):
    for name in ("plan-a", "plan-gone"):
        (plan_dir / f"{name}.manifest.json").write_text(
            json.dumps({"epics": {}, "stories": {}})
        )
    os.utime(plan_dir / "plan-a.manifest.json", (2_000_000, 2_000_000))

    real_store = dashboard_helpers._store
    real_records = real_store.records()

    class _VanishingRecords:
        def get_manifest_updated_at(self, namespace, plan_name):
            if plan_name == "plan-gone":
                return None
            return real_records.get_manifest_updated_at(namespace, plan_name)

    class _VanishingStore:
        def __getattr__(self, attr):
            return getattr(real_store, attr)

        def records(self):
            return _VanishingRecords()

    monkeypatch.setattr(dashboard_helpers, "_store", _VanishingStore())

    res = client.get("/api/plans")

    assert res.status_code == 200
    plans = res.json()["plans"]
    assert [p["name"] for p in plans] == ["plan-a", "plan-gone"]
    assert plans[-1]["updated_at"] == 0.0
