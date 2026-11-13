"""``manifest_path`` is removed from the Store Protocol and both implementations.

ADR 0012 decision 1: no store method returns a filesystem path. The Protocol,
``FileStore`` and ``SqliteStore`` must not expose the attribute, and the file
store still round-trips a manifest through ``get_manifest`` and
``save_manifest`` into the plans directory the server module patches.
"""

import pytest

from pipeline import server
from pipeline.store import FileStore, Store
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = "removal-plan"
NS = DEFAULT_NAMESPACE


@pytest.mark.parametrize("target", [Store, FileStore, SqliteStore])
def test_store_surface_has_no_manifest_path_attribute(target):
    assert not hasattr(target, "manifest_path")


def test_file_store_round_trips_manifest_into_patched_plans_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    store = FileStore()
    manifest = {"stories": {"S1": {"status": "pending"}}}

    store.save_manifest(PLAN, manifest)

    assert (tmp_path / f"{PLAN}.manifest.json").is_file()
    assert store.get_manifest(PLAN) == manifest


def test_manifest_updated_at_is_a_float_for_a_saved_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    store = FileStore()
    store.save_manifest(PLAN, {"stories": {}})

    updated_at = store.records().get_manifest_updated_at(NS, PLAN)

    assert isinstance(updated_at, float)


def test_manifest_updated_at_is_none_for_a_missing_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)

    assert FileStore().records().get_manifest_updated_at(NS, PLAN) is None
