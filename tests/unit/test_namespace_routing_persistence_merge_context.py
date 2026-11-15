"""Request-scoped routing of the persistence, merge and dispatch-context paths.

Each path writes or reads one record-store partition. With ``a/b`` bound by
the request, the partition is ``a/b``; with nothing bound it is the default
partition, exactly as before. A store that cannot hold the bound namespace
refuses the call; the call is never redirected to the default partition.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import pipeline.server
import pipeline.store
from pipeline import context_sources, dispatch_context, merge, persistence
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError

NAMESPACE = "a/b"
PLAN = "plan-x"
NOTIFICATION = {
    "ts": "2026-01-02T03:04:05+00:00",
    "plan": PLAN,
    "message": "merge held",
    "story_key": None,
    "severity": "info",
    "event": "merge_held",
    "dedup_key": "evt-1",
}
DECISION = {"ruling": "hold", "decided_by": "overlord"}
TEXT = "context from the bound partition\n"
DIGEST = "sha256:" + hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
OTHER_TEXT = "context the directory holds now\n"


class _RecordStore:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


class _FileStoreStub(pipeline.store.FileStore):
    """Stands in for the file store: its layout has only the default partition."""

    def __init__(self) -> None:
        pass


class _LegacyDecisionStore:
    """A store with the legacy surface only and no record view."""

    def __init__(self) -> None:
        self.decisions: list[tuple[str, dict]] = []

    def append_decision(self, plan_name: str, record: dict) -> None:
        self.decisions.append((plan_name, record))


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> InMemoryRecordStore:
    """Hand the request paths an in-memory record store for one test."""
    store = InMemoryRecordStore()
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(store))
    return store


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the file layout at an empty directory so a stray write is visible."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return directory


# -- notifications ------------------------------------------------------------


def test_bound_notification_lands_in_the_bound_partition(
    records: InMemoryRecordStore,
) -> None:
    """With ``a/b`` bound the notification is appended to that partition only."""
    with bind_namespace(NAMESPACE):
        persistence._append_notification(PLAN, dict(NOTIFICATION))

    assert len(records.get_notification_history(NAMESPACE, PLAN)[0]) == 1
    assert records.get_notification_history(DEFAULT_NAMESPACE, PLAN)[0] == []


def test_unbound_notification_lands_in_the_default_partition(
    records: InMemoryRecordStore,
) -> None:
    """Unbound, the notification is appended to the default partition, as before."""
    persistence._append_notification(PLAN, dict(NOTIFICATION))

    assert len(records.get_notification_history(DEFAULT_NAMESPACE, PLAN)[0]) == 1
    assert records.get_notification_history(NAMESPACE, PLAN)[0] == []


def test_bound_notification_without_a_record_view_is_refused(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in with no record view cannot hold ``a/b``: refused, nothing written."""
    monkeypatch.setattr(pipeline.server, "_store", object())

    with bind_namespace(NAMESPACE), pytest.raises(UnknownNamespaceError):
        persistence._append_notification(PLAN, dict(NOTIFICATION))

    assert list(plan_dir.iterdir()) == []


# -- merge adjudication decisions ---------------------------------------------


def test_bound_adjudication_decision_lands_in_the_bound_partition(
    records: InMemoryRecordStore,
) -> None:
    """With ``a/b`` bound the merge ruling is recorded in that partition only."""
    with bind_namespace(NAMESPACE):
        merge._append_adjudication_decision(PLAN, dict(DECISION))

    assert len(records.get_decisions(NAMESPACE, PLAN)) == 1
    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == []


def test_unbound_adjudication_decision_lands_in_the_default_partition(
    records: InMemoryRecordStore,
) -> None:
    """Unbound, the merge ruling is recorded in the default partition, as before."""
    merge._append_adjudication_decision(PLAN, dict(DECISION))

    assert len(records.get_decisions(DEFAULT_NAMESPACE, PLAN)) == 1
    assert records.get_decisions(NAMESPACE, PLAN) == []


def test_bound_adjudication_decision_on_the_file_store_is_refused(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file layout holds only the default partition: refused, not written there."""
    monkeypatch.setattr(pipeline.server, "_store", _FileStoreStub())

    with bind_namespace(NAMESPACE), pytest.raises(UnknownNamespaceError):
        merge._append_adjudication_decision(PLAN, dict(DECISION))

    assert list(plan_dir.iterdir()) == []


def test_bound_adjudication_decision_on_a_legacy_store_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy store that cannot name a namespace refuses the bound ruling."""
    legacy = _LegacyDecisionStore()
    monkeypatch.setattr(pipeline.server, "_store", legacy)

    with bind_namespace(NAMESPACE), pytest.raises(UnknownNamespaceError):
        merge._append_adjudication_decision(PLAN, dict(DECISION))

    assert legacy.decisions == []


# -- dispatch context ---------------------------------------------------------


def _source_for(directory: Path) -> dict:
    return {
        "name": "src",
        "provider": "dir",
        "uri": str(directory),
        "phases": ["dispatch"],
        "required": True,
    }


def _recorded() -> dict:
    return {"digest": DIGEST, "bytes": len(TEXT.encode("utf-8"))}


@pytest.fixture
def changed_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the source resolve to text that no longer matches the recorded digest."""
    monkeypatch.setattr(
        context_sources, "resolve_dir_source", lambda source: {"text": OTHER_TEXT}
    )
    return tmp_path


def test_bound_recorded_context_is_read_from_the_bound_partition(
    records: InMemoryRecordStore, changed_directory: Path
) -> None:
    """With ``a/b`` bound the recorded snapshot is read back from that partition."""
    records.put_context_snapshot(NAMESPACE, DIGEST, TEXT, {})

    with bind_namespace(NAMESPACE):
        loaded = dispatch_context._load_recorded_context(
            _source_for(changed_directory), _recorded()
        )

    assert loaded["text"] == TEXT


def test_unbound_recorded_context_is_read_from_the_default_partition(
    records: InMemoryRecordStore, changed_directory: Path
) -> None:
    """Unbound, the recorded snapshot is read from the default partition, as before."""
    records.put_context_snapshot(DEFAULT_NAMESPACE, DIGEST, TEXT, {})

    loaded = dispatch_context._load_recorded_context(
        _source_for(changed_directory), _recorded()
    )

    assert loaded["text"] == TEXT


def test_bound_recorded_context_is_not_read_from_the_default_partition(
    records: InMemoryRecordStore, changed_directory: Path
) -> None:
    """A snapshot held only in the default partition is not served to a bound request."""
    records.put_context_snapshot(DEFAULT_NAMESPACE, DIGEST, TEXT, {})

    with bind_namespace(NAMESPACE), pytest.raises(context_sources.ContextSourceError):
        dispatch_context._load_recorded_context(
            _source_for(changed_directory), _recorded()
        )


def test_bound_required_context_failure_blocks_the_manifest_in_the_bound_partition(
    records: InMemoryRecordStore, tmp_path: Path
) -> None:
    """A blocked dispatch records its manifest under the bound namespace, not default."""
    missing = tmp_path / "missing"
    manifest = {
        "context": {"sources": [_source_for(missing)]},
        "stories": {},
    }
    story = {"context_digests": {}}

    with bind_namespace(NAMESPACE):
        _text, blocked = dispatch_context._step_resolve_context(
            PLAN, "S1", story, manifest
        )

    assert blocked is not None and blocked["ok"] is False
    assert records.get_manifest(NAMESPACE, PLAN) is not None
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


def test_bound_context_snapshot_is_written_to_the_bound_partition(
    records: InMemoryRecordStore,
) -> None:
    """With ``a/b`` bound a fetched snapshot is stored in that partition only."""
    with bind_namespace(NAMESPACE):
        digest = context_sources.snapshot_context(TEXT)

    assert records.get_context_snapshot(NAMESPACE, digest) is not None
    assert records.get_context_snapshot(DEFAULT_NAMESPACE, digest) is None


def test_unbound_context_snapshot_is_written_to_the_default_partition(
    records: InMemoryRecordStore,
) -> None:
    """Unbound, a fetched snapshot is stored in the default partition, as before."""
    digest = context_sources.snapshot_context(TEXT)

    assert records.get_context_snapshot(DEFAULT_NAMESPACE, digest) is not None
    assert records.get_context_snapshot(NAMESPACE, digest) is None


# -- merge manifest paths -----------------------------------------------------


def test_bound_pause_is_refused_with_an_explicit_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound pause is denied explicitly; the default manifest is never touched."""
    store = _FileStoreStub()
    monkeypatch.setattr(pipeline.server, "_store", store)

    with bind_namespace(NAMESPACE):
        result = merge._set_plan_paused(PLAN, True)

    assert result == {"ok": False, "error": "unknown namespace"}


def test_bound_approve_merge_is_refused_with_an_explicit_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound merge approval is denied explicitly; no default manifest is read or written."""
    store = _FileStoreStub()
    monkeypatch.setattr(pipeline.server, "_store", store)

    with bind_namespace(NAMESPACE):
        result = merge._approve_merge_impl(PLAN, "S1")

    assert result == {"ok": False, "error": "unknown namespace"}
