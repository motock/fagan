"""The human resolution is linked to the ruling that is actually on record.

``pipeline.triage_outcomes`` pairs an operator's action with the machine's
ruling for the same story. When the ruling and the resolution live in the
record store instead of a legacy ``<plan>.decisions.json`` file, the pairing
must still work: the resolution must find the ruling the store holds and
must be written back to the same store.

Two kinds of ruling can sit on a story:

* a ``failure triage`` ruling, which carries an ``action`` and a ``park_class``;
* a ``high-risk merge adjudication`` ruling, which carries a ``ruling``
  (``proceed`` or ``park``) and no park class of its own - its class is
  ``high_risk_merge``.

These tests use an in-memory store patched in as ``pipeline.server._store``,
so the assertions are about the store's contents and never about a file.
"""

from __future__ import annotations

import pytest

from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline import triage_outcomes as to
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN = "TRIAGE-STORE-PLAN"
STORY = "TRIAGE-STORE-S1"
DECIDED_AT = "2026-11-05T09:00:00+00:00"


class _FakeRecords:
    """A namespaced record view that keeps its decisions in memory."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.decisions: list[tuple[str, str, dict]] = []

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


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", d)
    monkeypatch.setattr(persistence_mod, "PLAN_DIR", d)
    return d


@pytest.fixture
def store(monkeypatch):
    """An in-memory store patched in as the configured store."""
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)
    return records


def _seed(records, record: dict) -> None:
    records.decisions.append((DEFAULT_NAMESPACE, PLAN, dict(record)))


def _triage_ruling(**overrides) -> dict:
    record = {
        "question": "failure triage",
        "story_key": STORY,
        "action": "park_for_human",
        "park_class": "rework",
        "decided_by": "overlord-triage",
        "decided_at": DECIDED_AT,
    }
    record.update(overrides)
    return record


def _adjudication(ruling: str = "park", **overrides) -> dict:
    record = {
        "question": "high-risk merge adjudication",
        "story_key": STORY,
        "ruling": ruling,
        "rationale": "needs a human",
        "decided_by": "overlord",
        "decided_at": DECIDED_AT,
    }
    record.update(overrides)
    return record


def _resolve(**overrides) -> dict:
    kwargs = {
        "human_action": "park",
        "new_status": "in_progress",
        "principal": "operator",
    }
    kwargs.update(overrides)
    return to.record_human_resolution(PLAN, STORY, **kwargs)


# --- the resolution links to the ruling the store holds ---------------------


def test_a_store_ruling_is_the_one_the_resolution_links_to(store):
    _seed(store, _triage_ruling())

    record = _resolve()

    assert record["ruling_ref"] == DECIDED_AT
    assert record["ruling_action"] == "park_for_human"
    assert record["park_class"] == "rework"


def test_a_store_adjudication_is_the_one_the_resolution_links_to(store):
    _seed(store, _adjudication(ruling="park"))

    record = _resolve()

    assert record["ruling_ref"] == DECIDED_AT
    assert record["ruling_action"] == "park"
    assert record["park_class"] == "high_risk_merge"


def test_a_proceed_adjudication_copies_proceed_as_the_ruling_action(store):
    _seed(store, _adjudication(ruling="proceed"))

    record = _resolve(human_action="merge_held")

    assert record["ruling_action"] == "proceed"
    assert record["park_class"] == "high_risk_merge"


def test_the_latest_ruling_on_the_story_is_the_one_that_is_linked(store):
    _seed(store, _triage_ruling(decided_at="2026-11-01T00:00:00+00:00"))
    _seed(store, _adjudication(ruling="park", decided_at=DECIDED_AT))

    record = _resolve()

    assert record["ruling_ref"] == DECIDED_AT
    assert record["park_class"] == "high_risk_merge"


def test_a_failure_triage_ruling_after_an_adjudication_is_the_latest(store):
    _seed(store, _adjudication(ruling="park", decided_at="2026-11-01T00:00:00+00:00"))
    _seed(store, _triage_ruling(decided_at=DECIDED_AT))

    record = _resolve()

    assert record["ruling_ref"] == DECIDED_AT
    assert record["park_class"] == "rework"


def test_a_ruling_for_another_story_is_not_linked(store):
    _seed(store, _triage_ruling(story_key="OTHER-STORY"))

    record = _resolve()

    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None
    assert record["park_class"] is None


def test_an_execution_record_is_not_a_ruling(store):
    _seed(
        store,
        {
            "question": "failure triage execution",
            "story_key": STORY,
            "action": "park_for_human",
            "park_class": "rework",
            "decided_at": DECIDED_AT,
        },
    )

    record = _resolve()

    assert record["ruling_ref"] is None


# --- the resolution is written back to the same store -----------------------


    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None
    assert record["park_class"] is None
    assert record["question"] == "human resolution"
    _seed(store, _triage_ruling())

    _resolve()

    assert not persistence_mod._decisions_path(PLAN).exists()


def test_the_file_store_path_writes_through_the_legacy_writer(plan_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_store", FileStore(), raising=False)
    seen: list[tuple[str, dict]] = []

    def spy(plan_name, record):
        seen.append((plan_name, record))

    monkeypatch.setattr(persistence_mod, "_append_decision", spy)

    record = _resolve()

    assert seen and seen[0][0] == PLAN
    assert seen[0][1]["question"] == "human resolution"
    assert record["question"] == "human resolution"


# --- negative and boundary cases --------------------------------------------


def test_an_invalid_human_action_writes_nothing(store):
    _seed(store, _triage_ruling())

    with pytest.raises(ValueError, match="invalid human action"):
        _resolve(human_action="not_an_action")

    assert store.calls == []


def test_a_store_without_any_ruling_records_an_unlinked_resolution(store):
    record = _resolve()

    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None
    assert record["park_class"] is None
    assert record["question"] == "human resolution"


def test_an_adjudication_without_a_ruling_field_is_not_linked(store):
    _seed(store, _adjudication(ruling=""))

    record = _resolve()

    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None
    assert record["park_class"] is None
