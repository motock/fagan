"""The record-store wrappers that select the namespace bound for the call.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so every manifest, journal and decision read or write below a tick has to
land in the namespace bound for that tick instead of in the default partition.
These tests pin that selection through the public wrappers: with nothing bound
the default partition is used, with a namespace bound that partition is used
and the default one is left untouched, and a store that cannot hold the bound
namespace is reported as refusing it rather than silently redirected.
"""

from __future__ import annotations

import pytest

from pipeline import bound_records
from pipeline.actor import bind_namespace
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "story-1"

WRAPPERS = (
    "get_manifest",
    "put_manifest",
    "get_journal",
    "append_journal",
    "append_decision",
    "get_decisions",
    "list_plans",
    "refuses_bound_namespace",
)


class _RecordStore:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


@pytest.fixture
def records() -> InMemoryRecordStore:
    """One in-memory record store, holding every partition of one test."""
    return InMemoryRecordStore()


@pytest.fixture
def store(records: InMemoryRecordStore) -> _RecordStore:
    """The store facade the wrappers are called with."""
    return _RecordStore(records)


# -- the default partition ---------------------------------------------------


def test_unbound_put_manifest_writes_the_default_partition(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """With nothing bound the manifest lands in the default partition."""
    bound_records.put_manifest(store, PLAN, {"epics": []})

    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) == {"epics": []}
    assert records.get_manifest(NAMESPACE, PLAN) is None


def test_unbound_list_plans_lists_the_default_partition(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """With nothing bound the plan names come from the default partition."""
    records.put_manifest(DEFAULT_NAMESPACE, "plan-default", {})
    records.put_manifest(NAMESPACE, "plan-bound", {})

    assert bound_records.list_plans(store) == ["plan-default"]


def test_unbound_journal_and_decisions_land_in_the_default_partition(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """With nothing bound the journal and decisions land in the default one."""
    bound_records.append_journal(store, PLAN, STORY, {"event": "tick"})
    bound_records.append_decision(store, PLAN, {"decision": "go"})

    assert bound_records.get_journal(store, PLAN, STORY) == [{"event": "tick"}]
    assert bound_records.get_decisions(store, PLAN) == [{"decision": "go"}]
    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [{"event": "tick"}]
    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [{"decision": "go"}]
    assert records.get_journal(NAMESPACE, PLAN, STORY) == []
    assert records.get_decisions(NAMESPACE, PLAN) == []


def test_get_manifest_of_an_unknown_plan_is_none(store: _RecordStore) -> None:
    """A plan the partition does not hold has no manifest."""
    assert bound_records.get_manifest(store, PLAN) is None


# -- the bound partition ------------------------------------------------------


def test_bound_put_manifest_writes_the_bound_partition_only(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """Bound, the manifest lands in the bound partition and not in the default."""
    with bind_namespace(NAMESPACE):
        bound_records.put_manifest(store, PLAN, {"epics": []})

    assert records.get_manifest(NAMESPACE, PLAN) == {"epics": []}
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


def test_bound_get_manifest_reads_the_bound_partition_only(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """Bound, the manifest read is the one in the bound partition."""
    records.put_manifest(NAMESPACE, PLAN, {"where": "bound"})
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, {"where": "default"})

    with bind_namespace(NAMESPACE):
        assert bound_records.get_manifest(store, PLAN) == {"where": "bound"}


def test_bound_journal_is_written_to_and_read_from_the_bound_partition(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """Bound, the journal record lands in the bound partition only."""
    with bind_namespace(NAMESPACE):
        bound_records.append_journal(store, PLAN, STORY, {"event": "tick"})
        assert bound_records.get_journal(store, PLAN, STORY) == [{"event": "tick"}]

    assert records.get_journal(NAMESPACE, PLAN, STORY) == [{"event": "tick"}]
    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []


def test_bound_decisions_are_written_to_and_read_from_the_bound_partition(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """Bound, the decision record lands in the bound partition only."""
    with bind_namespace(NAMESPACE):
        bound_records.append_decision(store, PLAN, {"decision": "go"})
        assert bound_records.get_decisions(store, PLAN) == [{"decision": "go"}]

    assert records.get_decisions(NAMESPACE, PLAN) == [{"decision": "go"}]
    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == []


def test_bound_list_plans_lists_the_bound_partition_only(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """Bound, the plan names come from the bound partition only."""
    records.put_manifest(NAMESPACE, "plan-bound", {})
    records.put_manifest(DEFAULT_NAMESPACE, "plan-default", {})

    with bind_namespace(NAMESPACE):
        assert bound_records.list_plans(store) == ["plan-bound"]


def test_the_bound_partition_is_selected_per_call(
    store: _RecordStore, records: InMemoryRecordStore
) -> None:
    """The partition is chosen when the call is made, not when it is bound."""
    with bind_namespace(NAMESPACE):
        with bind_namespace(OTHER_NAMESPACE):
            bound_records.put_manifest(store, PLAN, {"where": "inner"})
        bound_records.put_manifest(store, "plan-outer", {"where": "outer"})

    assert records.get_manifest(OTHER_NAMESPACE, PLAN) == {"where": "inner"}
    assert records.get_manifest(NAMESPACE, "plan-outer") == {"where": "outer"}
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


# -- stores that cannot hold the bound namespace -----------------------------


def test_refuses_bound_namespace_is_false_for_the_file_store_unbound() -> None:
    """Nothing bound: the file layout holds the partition that is asked for."""
    assert bound_records.refuses_bound_namespace(FileStore()) is False


def test_refuses_bound_namespace_is_true_for_the_file_store_when_bound() -> None:
    """The file layout holds the default partition only."""
    with bind_namespace(NAMESPACE):
        assert bound_records.refuses_bound_namespace(FileStore()) is True


def test_refuses_bound_namespace_is_false_for_the_file_store_on_the_default() -> None:
    """The default partition is never refused, bound or not."""
    with bind_namespace(DEFAULT_NAMESPACE):
        assert bound_records.refuses_bound_namespace(FileStore()) is False


def test_refuses_bound_namespace_is_false_for_a_record_store_when_bound(
    store: _RecordStore,
) -> None:
    """A record store selects the namespace per call, so it never refuses."""
    with bind_namespace(NAMESPACE):
        assert bound_records.refuses_bound_namespace(store) is False


# -- refused input ------------------------------------------------------------


def test_put_manifest_refuses_an_invalid_plan_name(store: _RecordStore) -> None:
    """A plan name the store refuses is refused through the wrapper too."""
    with pytest.raises(InvalidRecordKeyError, match="invalid plan name"):
        bound_records.put_manifest(store, "bad/plan", {})


def test_append_journal_refuses_an_invalid_story_key(store: _RecordStore) -> None:
    """A story key the store refuses is refused through the wrapper too."""
    with pytest.raises(InvalidRecordKeyError, match="invalid story key"):
        bound_records.append_journal(store, PLAN, "bad/story", {})


def test_put_manifest_refuses_a_manifest_that_is_not_a_dict(
    store: _RecordStore,
) -> None:
    """A manifest that is not a dict is refused by the store."""
    with pytest.raises(TypeError, match="manifest must be a dict"):
        bound_records.put_manifest(store, PLAN, ["not", "a", "dict"])


def test_a_bound_namespace_the_store_refuses_is_reported_by_the_store(
    store: _RecordStore,
) -> None:
    """The bound namespace is passed on as it is, so the store judges it."""
    with bind_namespace("bad namespace"), pytest.raises(
        InvalidRecordKeyError, match="invalid namespace"
    ):
        bound_records.put_manifest(store, PLAN, {})


def test_binding_an_empty_namespace_is_refused() -> None:
    """There is no such thing as an empty namespace to select."""
    with pytest.raises(
        ValueError, match="namespace must be a non-empty string"
    ), bind_namespace(""):
        pass


# -- the module surface -------------------------------------------------------


def test_the_module_exposes_the_record_wrappers() -> None:
    """Every wrapper the callers use is a callable on the module."""
    for name in WRAPPERS:
        assert callable(getattr(bound_records, name)), name
