"""The plan-completion summary reads story journals from the record store.

``_with_journals`` used to open ``<plan>.<story>.journal.json`` itself through
its own module copy of ``PLAN_DIR``; it now asks the record store for each
story's journal (``build_store().records().get_journal("default", ...)``), so the
summary sees exactly the journals the writers wrote, wherever the store keeps
them.
"""

from __future__ import annotations

import copy

from pipeline import plan_completion
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import InvalidRecordKeyError

_PLAN = "journalled-plan"


class _RejectingRecords:
    """Records view that rejects one story key, like a strict store would."""

    def __init__(self, inner, rejected):
        self._inner = inner
        self._rejected = rejected

    def get_journal(self, namespace, plan_name, story_key):
        if story_key == self._rejected:
            raise InvalidRecordKeyError(story_key)
        return self._inner.get_journal(namespace, plan_name, story_key)


class _Store:
    """Minimal ``Store`` surface: only ``records()`` is used here."""

    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


def _manifest(*keys):
    return {
        "plan": _PLAN,
        "stories": {
            key: {"id": key, "status": "done", "title": f"story {key}"} for key in keys
        },
    }


def _patch_store(monkeypatch, records):
    """Point ``plan_completion``'s store lookup at ``records``."""
    monkeypatch.setattr(
        plan_completion, "build_store", lambda *args, **kwargs: _Store(records)
    )


def _records_with(entries_by_key, rejected=None):
    records = InMemoryRecordStore()
    for key, entries in entries_by_key.items():
        for entry in entries:
            records.append_journal("default", _PLAN, key, entry)
    if rejected is not None:
        return _RejectingRecords(records, rejected)
    return records


def test_should_attach_each_story_journal_from_the_record_store(monkeypatch):
    records = _records_with(
        {
            "S1": [{"ts": "t1", "kind": "note"}],
            "S2": [{"ts": "t2", "kind": "note"}, {"ts": "t3", "kind": "note"}],
        }
    )
    _patch_store(monkeypatch, records)

    result = plan_completion._with_journals(_PLAN, _manifest("S1", "S2"))

    assert result["stories"]["S1"]["journal"] == [{"ts": "t1", "kind": "note"}]
    assert result["stories"]["S2"]["journal"] == [
        {"ts": "t2", "kind": "note"},
        {"ts": "t3", "kind": "note"},
    ]


def test_should_return_the_manifest_unchanged_when_no_story_has_a_journal(monkeypatch):
    _patch_store(monkeypatch, _records_with({}))
    manifest = _manifest("S1", "S2")

    result = plan_completion._with_journals(_PLAN, manifest)

    assert result is manifest


def test_should_not_mutate_the_caller_manifest(monkeypatch):
    _patch_store(monkeypatch, _records_with({"S1": [{"ts": "t1", "kind": "note"}]}))
    manifest = _manifest("S1")
    before = copy.deepcopy(manifest)

    result = plan_completion._with_journals(_PLAN, manifest)

    assert manifest == before
    assert "journal" not in manifest["stories"]["S1"]
    assert result["stories"]["S1"]["journal"] == [{"ts": "t1", "kind": "note"}]


def test_should_skip_a_story_key_the_store_rejects(monkeypatch):
    records = _records_with(
        {"S1": [{"ts": "t1", "kind": "note"}]}, rejected="_S2"
    )
    _patch_store(monkeypatch, records)

    result = plan_completion._with_journals(_PLAN, _manifest("S1", "_S2"))

    assert result["stories"]["S1"]["journal"] == [{"ts": "t1", "kind": "note"}]
    assert "journal" not in result["stories"]["_S2"]
