"""Decision, usage and outbox writes follow the request namespace.

Each path that used to name ``DEFAULT_NAMESPACE`` for every write now selects
the namespace bound for the request. A write under a bound namespace lands in
that namespace and is invisible from the default one; an unbound write lands in
the default namespace exactly as before. An explicit namespace argument, where
the function takes one, still wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pipeline.server
import pipeline.telemetry_usage
from pipeline import decision_records, notification_outbox, paths, persistence
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, LEGACY_OUTBOX_CHANNEL
from pipeline.usage_record import record_usage, usage_scope

NAMESPACE = "a/b"
PLAN = "plan-x"


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the decision stream at a temp directory for one test."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    return directory


class _RecordView:
    """A live store whose record view is one in-memory record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> InMemoryRecordStore:
    """Install an in-memory record store as the live server store."""
    store = InMemoryRecordStore()
    monkeypatch.setattr(pipeline.server, "_store", _RecordView(store))
    return store


def _sha(seed: str = "a") -> str:
    return (seed * 64)[:64]


def _decision() -> dict:
    """A valid caller record for the decision stream."""
    return {
        "decision_id": "0f" * 16,
        "class": "high_risk_merge",
        "subject": {"plan": PLAN, "story": "s1", "pr": "42", "sha": _sha("b")},
        "evidence": [{"kind": "test_run", "ref": "pytest -q", "sha256": _sha("c")}],
        "rule": {
            "layer_shas": [_sha("d")],
            "persona_sha": _sha("f"),
            "policy_sha": _sha("1"),
            "model": "claude-sonnet-4",
        },
        "alternatives": ["wait for CI"],
        "ruling": "merge: CI green",
        "tier": "auto+notify",
        "reversible": True,
        "outcome": "",
    }


# -- decision records --------------------------------------------------------


def test_bound_decision_record_is_read_back_under_its_namespace(
    plan_dir: Path,
) -> None:
    """A record appended with ``a/b`` bound is read under ``a/b``."""
    with bind_namespace(NAMESPACE):
        decision_records.append_decision_record(PLAN, _decision())

    assert len(decision_records.read_decision_records(PLAN, namespace=NAMESPACE)) == 1


def test_bound_decision_record_is_invisible_from_the_default_namespace(
    plan_dir: Path,
) -> None:
    """The default read does not see a record written under a bound namespace."""
    with bind_namespace(NAMESPACE):
        decision_records.append_decision_record(PLAN, _decision())

    assert decision_records.read_decision_records(PLAN) == []


def test_unbound_decision_record_lands_in_the_default_namespace(
    plan_dir: Path,
) -> None:
    """With nothing bound the record is the default namespace's, as before."""
    decision_records.append_decision_record(PLAN, _decision())

    assert len(decision_records.read_decision_records(PLAN)) == 1
    assert decision_records.read_decision_records(PLAN, namespace=NAMESPACE) == []


def test_explicit_namespace_argument_wins_over_the_bound_one(plan_dir: Path) -> None:
    """A caller that names a namespace still writes there, whatever is bound."""
    with bind_namespace(NAMESPACE):
        decision_records.append_decision_record(
            PLAN, _decision(), namespace=DEFAULT_NAMESPACE
        )

    assert len(decision_records.read_decision_records(PLAN)) == 1
    assert decision_records.read_decision_records(PLAN, namespace=NAMESPACE) == []


def test_bound_decision_read_returns_only_the_bound_namespace(plan_dir: Path) -> None:
    """A default write and a bound write share a stream; each read filters to its own."""
    decision_records.append_decision_record(PLAN, _decision())
    with bind_namespace(NAMESPACE):
        decision_records.append_decision_record(PLAN, _decision())

    with bind_namespace(NAMESPACE):
        bound = decision_records.read_decision_records(PLAN)

    assert [record["namespace"] for record in bound] == [NAMESPACE]


# -- usage -------------------------------------------------------------------


@pytest.fixture
def no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the telemetry exporter out of these record-routing tests."""
    monkeypatch.setattr(pipeline.telemetry_usage, "record", lambda record: None)


def _write_usage() -> None:
    with usage_scope(PLAN, "story-1", attempt=1):
        record_usage(role="dispatch", backend="claude", model="claude-sonnet-5-5")


def test_bound_usage_record_lands_in_its_namespace(
    memory: InMemoryRecordStore, no_telemetry: None
) -> None:
    """Usage written with ``a/b`` bound is stored under ``a/b`` only."""
    with bind_namespace(NAMESPACE):
        _write_usage()

    assert len(memory.get_usage(NAMESPACE, PLAN)) == 1
    assert memory.get_usage(DEFAULT_NAMESPACE, PLAN) == []


def test_unbound_usage_record_lands_in_the_default_namespace(
    memory: InMemoryRecordStore, no_telemetry: None
) -> None:
    """With nothing bound the usage is the default namespace's, as before."""
    _write_usage()

    assert len(memory.get_usage(DEFAULT_NAMESPACE, PLAN)) == 1
    assert memory.get_usage(NAMESPACE, PLAN) == []


# -- notification outbox -----------------------------------------------------

EVENT = {
    "plan": PLAN,
    "payload": {"event": "plan_completed"},
    "message": "done",
}


@pytest.fixture
def outbox_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the legacy outbox on so the sink spools one record per event."""
    monkeypatch.setenv(notification_outbox.OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(notification_outbox.OUTBOX_EVENTS_ENV, "plan_completed")


def test_bound_outbox_entry_lands_in_its_namespace(
    memory: InMemoryRecordStore, outbox_enabled: None
) -> None:
    """A notification spooled with ``a/b`` bound is queued under ``a/b`` only."""
    with bind_namespace(NAMESPACE):
        notification_outbox.outbox_sink(EVENT)

    assert len(memory.read_outbox(NAMESPACE, PLAN, LEGACY_OUTBOX_CHANNEL)) == 1
    assert memory.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY_OUTBOX_CHANNEL) == []


def test_unbound_outbox_entry_lands_in_the_default_namespace(
    memory: InMemoryRecordStore, outbox_enabled: None
) -> None:
    """With nothing bound the entry is queued under the default namespace, as before."""
    notification_outbox.outbox_sink(EVENT)

    assert len(memory.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY_OUTBOX_CHANNEL)) == 1
    assert memory.read_outbox(NAMESPACE, PLAN, LEGACY_OUTBOX_CHANNEL) == []
