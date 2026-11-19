"""The decision log is the one seam every ruling and resolution goes through.

``pipeline.decision_log`` is the store-first seam between the modules that
write decisions (the triage ruling, the human resolution, the high-risk merge
adjudication) and the record store (ADR 0012 stage B). The contract these
tests pin down:

* ``append_decision`` writes through the store's namespaced record view when
  the configured store has one, so a deployment whose store owns its decision
  storage keeps the record instead of losing it to a JSON file on this host;
* the file store keeps the one legacy writer, so the record still lands in
  ``<plan>.decisions.json``;
* ``read_decisions`` mirrors the writer's choice exactly - the legacy file for
  a file store (or a store with no record view), the store's records
  otherwise - so a reader never sees a different stream than the writer wrote;
* a record that is not a dict is skipped on read, so one malformed entry
  cannot take the whole stream down.
"""

from __future__ import annotations

import json

import pytest

from pipeline import decision_log
from pipeline import merge as merge_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN = "DECISION-LOG-PLAN"
STORY_KEY = "DECISION-LOG-S1"


class _FakeRecords:
    """A namespaced record view that keeps its decisions in memory."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.decisions: list[tuple[str, str, object]] = []

    def append_decision(self, namespace, plan_name, record):
        self.calls.append((namespace, plan_name, dict(record)))
        self.decisions.append((namespace, plan_name, dict(record)))

    def get_decisions(self, namespace, plan_name):
        return [
            record
            for ns, name, record in self.decisions
            if ns == namespace and name == plan_name
        ]


class _FakeStore:
    """A stand-in store with a namespaced record view."""

    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


class _NoRecordsStore:
    """A stand-in store with no record view at all."""


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point both the server and persistence plan dirs at a temp directory."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", d)
    monkeypatch.setattr(persistence_mod, "PLAN_DIR", d)
    return d


def _legacy_path(plan_name: str = PLAN):
    return persistence_mod._decisions_path(plan_name)


def _ruling(decided_at: str = "2026-11-01T00:00:00+00:00") -> dict:
    return {
        "question": "failure triage",
        "story_key": STORY_KEY,
        "action": "park_for_human",
        "park_class": "rework",
        "decided_by": "overlord-triage",
        "decided_at": decided_at,
    }


# --- append: store first ----------------------------------------------------


def test_append_writes_through_the_stores_record_view(plan_dir, monkeypatch):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    decision_log.append_decision(PLAN, _ruling())

    assert records.calls == [(DEFAULT_NAMESPACE, PLAN, _ruling())]


def test_append_writes_no_legacy_file_when_the_store_owns_the_records(
    plan_dir, monkeypatch
):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    decision_log.append_decision(PLAN, _ruling())

    assert not _legacy_path().exists()


def test_append_delegates_to_the_merge_adjudication_writer(plan_dir, monkeypatch):
    seen: list[tuple[str, dict]] = []

    def spy(plan_name, record):
        seen.append((plan_name, record))

    monkeypatch.setattr(merge_mod, "_append_adjudication_decision", spy)

    decision_log.append_decision(PLAN, _ruling())

    assert seen == [(PLAN, _ruling())]


def test_the_file_store_keeps_the_record_in_the_plan_decisions_log(
    plan_dir, monkeypatch
):
    monkeypatch.setattr(server_mod, "_store", FileStore(), raising=False)

    decision_log.append_decision(PLAN, _ruling())

    assert json.loads(_legacy_path().read_text()) == [_ruling()]


def test_append_keeps_the_records_of_two_plans_apart(plan_dir, monkeypatch):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    decision_log.append_decision(PLAN, _ruling())
    decision_log.append_decision("OTHER-PLAN", _ruling())

    assert [name for _, name, _ in records.calls] == [PLAN, "OTHER-PLAN"]


# --- read: mirrors the writer's choice --------------------------------------


def test_read_returns_the_record_the_store_holds(plan_dir, monkeypatch):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)
    decision_log.append_decision(PLAN, _ruling())

    assert decision_log.read_decisions(PLAN) == [_ruling()]


def test_read_returns_the_records_in_append_order(plan_dir, monkeypatch):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    first = _ruling("2026-11-01T00:00:00+00:00")
    second = _ruling("2026-11-02T00:00:00+00:00")
    decision_log.append_decision(PLAN, first)
    decision_log.append_decision(PLAN, second)

    assert decision_log.read_decisions(PLAN) == [first, second]


def test_read_returns_an_empty_list_when_the_store_holds_nothing(
    plan_dir, monkeypatch
):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    assert decision_log.read_decisions(PLAN) == []


def test_read_skips_a_record_that_is_not_a_dict(plan_dir, monkeypatch):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)
    decision_log.append_decision(PLAN, _ruling())
    records.decisions.append((DEFAULT_NAMESPACE, PLAN, "not a record"))

    assert decision_log.read_decisions(PLAN) == [_ruling()]


def test_read_of_the_file_store_reads_the_legacy_decisions_log(plan_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_store", FileStore(), raising=False)
    decision_log.append_decision(PLAN, _ruling())

    assert decision_log.read_decisions(PLAN) == [_ruling()]


def test_read_of_the_file_store_with_no_file_is_an_empty_list(plan_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_store", FileStore(), raising=False)

    assert decision_log.read_decisions(PLAN) == []


def test_read_of_a_store_without_a_record_view_reads_the_legacy_log(
    plan_dir, monkeypatch
):
    monkeypatch.setattr(server_mod, "_store", _NoRecordsStore(), raising=False)
    persistence_mod._append_decision(PLAN, _ruling())

    assert decision_log.read_decisions(PLAN) == [_ruling()]
