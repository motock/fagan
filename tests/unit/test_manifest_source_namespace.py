"""The store-first manifest read answers from the *request's* namespace.

``pipeline.manifest_source`` is the single store-first read of a plan's
manifest and of the plan listing. It used to name the default partition
outright, so a request that carried a namespace still read the default
namespace's manifest - the wrong copy of the plan, for a store that holds
more than one namespace.

These tests pin the routed behaviour through the public functions:

* with a namespace bound for the request, the manifest and the plan listing
  come from that namespace;
* with nothing bound, the default namespace is read (the request namespace
  resolves to the default when nothing is bound);
* a plan stored in *another* namespace is not readable through a bound
  namespace;
* the two explicit failure contracts are unchanged - a store error propagates
  when ``fallback_on_error`` is false, and degrades to the file when it is
  true.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server
from pipeline import manifest_source
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

#: A second, segmented namespace the same store also holds manifests in.
PROBE_NAMESPACE = "default/probe"

#: A segmented namespace the store holds nothing in.
EMPTY_NAMESPACE = "default/empty"

#: The plan stored in the default namespace, and the one stored in the probe.
DEFAULT_PLAN = "alpha"
PROBE_PLAN = "beta"

DEFAULT_MANIFEST = {"title": "alpha", "role_config": {"reviewer": "a"}}
PROBE_MANIFEST = {"title": "beta", "role_config": {"reviewer": "b"}}


class _Store:
    """A store whose only surface is the record view."""

    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


class _FailingRecords:
    """A record view whose reads always fail."""

    def get_manifest(self, namespace, plan_name):
        raise RuntimeError("store is down")

    def list_plans(self, namespace):
        raise RuntimeError("store is down")


@pytest.fixture
def record_store():
    """A record store holding one manifest in each of two namespaces."""
    records = InMemoryRecordStore()
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, DEFAULT_MANIFEST)
    records.put_manifest(PROBE_NAMESPACE, PROBE_PLAN, PROBE_MANIFEST)
    return records


@pytest.fixture
def store(record_store, monkeypatch):
    """The same record store, installed as the selected store."""
    monkeypatch.setattr(pipeline.server, "_store", _Store(record_store), raising=False)
    return record_store


@pytest.fixture
def failing_store(monkeypatch):
    """A store whose record view fails every read."""
    monkeypatch.setattr(
        pipeline.server, "_store", _Store(_FailingRecords()), raising=False
    )
    return pipeline.server._store


def _write_plan_file(plans_root, plan_name, manifest):
    plans_root.mkdir(parents=True, exist_ok=True)
    (plans_root / f"{plan_name}.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_should_read_the_bound_namespace_manifest(store):
    """A bound namespace reads its own manifest, not the default one."""
    with bind_namespace(PROBE_NAMESPACE):
        manifest = manifest_source.read_plan_manifest(
            PROBE_PLAN, fallback_on_error=False
        )

    assert manifest == PROBE_MANIFEST


def test_should_read_the_default_manifest_when_no_namespace_is_bound(store):
    """With nothing bound, the request namespace is the default one."""
    manifest = manifest_source.read_plan_manifest(
        DEFAULT_PLAN, fallback_on_error=False
    )

    assert manifest == DEFAULT_MANIFEST


def test_should_not_read_a_plan_stored_in_another_namespace(store, tmp_path):
    """A plan stored in the default namespace is not readable once bound."""
    with bind_namespace(PROBE_NAMESPACE):
        manifest = manifest_source.read_plan_manifest(
            DEFAULT_PLAN, fallback_on_error=False, plans_root=tmp_path
        )

    assert manifest is None


def test_should_list_only_the_bound_namespace_plans(store, tmp_path):
    """The plan listing is the bound namespace's, not the whole store's."""
    with bind_namespace(PROBE_NAMESPACE):
        names = manifest_source.read_plan_names(
            fallback_on_error=False, plans_root=tmp_path
        )

    assert names == [PROBE_PLAN]


def test_should_list_no_plans_for_a_namespace_that_holds_none(store, tmp_path):
    """A namespace that holds no plans lists none, not the store's other plans."""
    with bind_namespace(EMPTY_NAMESPACE):
        names = manifest_source.read_plan_names(
            fallback_on_error=False, plans_root=tmp_path
        )

    assert names == []


def test_should_propagate_a_store_error_when_fallback_is_disabled(failing_store):
    """A failed store read fails closed when the caller says so."""
    with pytest.raises(RuntimeError, match="store is down"):
        manifest_source.read_plan_manifest(DEFAULT_PLAN, fallback_on_error=False)


def test_should_fall_back_to_the_file_when_fallback_is_enabled(
    failing_store, tmp_path
):
    """A failed store read degrades to the file when the caller says so."""
    _write_plan_file(tmp_path, DEFAULT_PLAN, DEFAULT_MANIFEST)

    manifest = manifest_source.read_plan_manifest(
        DEFAULT_PLAN, fallback_on_error=True, plans_root=tmp_path
    )

    assert manifest == DEFAULT_MANIFEST


def test_should_describe_the_read_as_the_request_namespace():
    """The module documents the store-first read as the request's namespace."""
    doc = manifest_source.__doc__ or ""

    assert "current_namespace" in doc
    assert "DEFAULT_NAMESPACE" not in doc
