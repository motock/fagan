"""Ingest's neighbour checks read other plans' manifests through the record store.

File-overlap advisories and the story-key collision check used to glob the
plan directory through ``cross_plan.load_manifests``. They now read
``_store.records().get_manifests("default")`` and drop the plan being ingested.
"""

from __future__ import annotations

import pytest

from pipeline import ingest
from pipeline import server as p


class _FakeRecords:
    def __init__(self, manifests):
        self._manifests = manifests
        self.namespaces = []

    def get_manifests(self, namespace):
        self.namespaces.append(namespace)
        return dict(self._manifests)


class _FakeStore:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


@pytest.fixture
def notes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: calls.append((plan, msg, kw))
    )
    return calls


@pytest.fixture
def records(monkeypatch):
    fake = _FakeRecords({})
    monkeypatch.setattr(p, "_store", _FakeStore(fake))
    return fake


def _with_manifests(records, manifests):
    records._manifests.update(manifests)


def _manifest(stories):
    return {"stories": stories}


def _neighbour(files, status="in_progress"):
    return _manifest({"K1": {"summary": "n", "status": status, "files": files}})


def test_should_advise_overlap_with_neighbour_read_from_records(records, notes):
    _with_manifests(records, {"other": _neighbour(["a.py", "b.py"])})

    advisories = ingest._file_overlap_advisories(
        "mine", {"S1": {"files": ["b.py", "c.py"]}}
    )

    assert advisories == [{"story": "S1", "plan": "other", "key": "K1", "files": ["b.py"]}]
    assert records.namespaces == ["default"]
    assert [call[2]["event"] for call in notes] == ["file_overlap"]


def test_should_not_advise_against_the_plan_being_ingested(records, notes):
    _with_manifests(records, {"mine": _neighbour(["b.py"])})

    advisories = ingest._file_overlap_advisories("mine", {"S1": {"files": ["b.py"]}})

    assert advisories == []
    assert notes == []


def test_should_return_no_advisories_without_other_plans(records, notes):
    advisories = ingest._file_overlap_advisories("mine", {"S1": {"files": ["b.py"]}})

    assert advisories == []
    assert notes == []


def test_should_not_advise_from_a_finished_neighbour(records, notes):
    _with_manifests(records, {"other": _neighbour(["b.py"], status="done")})

    advisories = ingest._file_overlap_advisories("mine", {"S1": {"files": ["b.py"]}})

    assert advisories == []
    assert notes == []


def _plan_with_key(key):
    story = {"summary": "S", "key": key}
    return {"epics": [{"summary": "E", "stories": [story]}]}


def test_should_refuse_key_held_by_another_plan_with_today_text(records):
    _with_manifests(records, {"other": _manifest({"K1": {"summary": "n", "status": "in_progress"}})})

    error = ingest._story_key_conflict_error("mine", _plan_with_key("K1"), None)

    assert error == (
        "story key 'K1' is already used by plan 'other' (story 'K1', status 'in_progress'); "
        "story keys share a branch and a worktree across plans, so choose a key "
        "unique to this plan or wait for that story to finish."
    )
    assert records.namespaces == ["default"]


def test_should_ignore_key_held_by_the_plan_being_ingested(records):
    _with_manifests(records, {"mine": _manifest({"K1": {"summary": "n", "status": "in_progress"}})})

    assert ingest._story_key_conflict_error("mine", _plan_with_key("K1"), None) is None


def test_should_allow_a_key_no_other_plan_holds(records):
    _with_manifests(records, {"other": _manifest({"K2": {"summary": "n", "status": "in_progress"}})})

    assert ingest._story_key_conflict_error("mine", _plan_with_key("K1"), None) is None
