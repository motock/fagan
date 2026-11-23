"""``list_plans`` and ``list_ingestable_plans`` read the caller's namespace.

The service answers these two reads for the namespace bound to the request, the
same way every other service read does. Before the fix both helpers were called
without a namespace and read the default partition, so a team bound to its own
namespace saw nothing.

The record stores serve plan drafts from the default partition only: a draft in
any other namespace is refused. So the bound-namespace cases below can only
stage plans as manifests; a bound caller's drafts are an empty set, and a
default-partition draft is never listed to a bound caller.
"""

from __future__ import annotations

import pytest

from pipeline import server as server_module
from pipeline import service as service_module
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

NAMESPACE = "acme/web"


class _RecordsStore:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


@pytest.fixture
def records() -> InMemoryRecordStore:
    return InMemoryRecordStore()


@pytest.fixture
def service(monkeypatch, records: InMemoryRecordStore) -> service_module.PipelineService:
    """A service whose record store is the in-memory store of this test."""
    monkeypatch.setattr(server_module, "_store", _RecordsStore(records))
    return service_module.PipelineService()


def _manifest() -> dict:
    return {"epics": [], "stories": {}}


def _draft() -> dict:
    return {"epics": []}


# -- list_plans ----------------------------------------------------------------


def test_bound_list_plans_returns_the_bound_namespace_plans_only(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """Bound to a team, list_plans names that team's plan and not the default one."""
    records.put_manifest(NAMESPACE, "alpha", _manifest())
    records.put_manifest(DEFAULT_NAMESPACE, "beta", _manifest())

    with bind_namespace(NAMESPACE):
        names = service.list_plans()

    assert names == ["alpha"]
    assert "beta" not in names


def test_unbound_list_plans_returns_the_default_namespace_plans_only(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """Unbound, list_plans keeps answering for the default namespace."""
    records.put_manifest(NAMESPACE, "alpha", _manifest())
    records.put_manifest(DEFAULT_NAMESPACE, "beta", _manifest())

    names = service.list_plans()

    assert names == ["beta"]
    assert "alpha" not in names


def test_bound_list_plans_does_not_list_a_default_draft(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """A draft staged in the default namespace is not listed to a bound team."""
    records.put_plan_draft(DEFAULT_NAMESPACE, "delta", _draft())

    with bind_namespace(NAMESPACE):
        names = service.list_plans()

    assert "delta" not in names


def test_bound_list_plans_of_an_empty_namespace_is_empty(
    service: service_module.PipelineService,
) -> None:
    """A bound namespace that holds no plans lists nothing."""
    with bind_namespace(NAMESPACE):
        assert service.list_plans() == []


# -- list_ingestable_plans -----------------------------------------------------


def test_unbound_list_ingestable_plans_returns_the_default_draft(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """Unbound, a staged default draft with no manifest is an ingest target."""
    records.put_plan_draft(DEFAULT_NAMESPACE, "delta", _draft())

    assert service.list_ingestable_plans() == ["delta"]


def test_bound_list_ingestable_plans_does_not_list_a_default_draft(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """A default-namespace draft is not an ingest target for a bound team."""
    records.put_plan_draft(DEFAULT_NAMESPACE, "delta", _draft())

    with bind_namespace(NAMESPACE):
        assert service.list_ingestable_plans() == []


def test_bound_list_ingestable_plans_of_an_empty_namespace_is_empty(
    service: service_module.PipelineService, records: InMemoryRecordStore
) -> None:
    """A bound namespace with no drafts has no ingest targets, and no error."""
    records.put_manifest(NAMESPACE, "alpha", _manifest())

    with bind_namespace(NAMESPACE):
        assert service.list_ingestable_plans() == []
