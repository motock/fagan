"""The record views read journals and decisions from the directory they write to.

``append_journal`` / ``append_decision`` on a record view write through
``pipeline.persistence``, which resolves the plan directory from its *own*
module-level binding, while the record view used to read back through the legacy
``FileStore`` reader, which resolves ``pipeline.server.PLAN_DIR``.  The two
bindings are the same object in production, but they are patched independently
in tests, so a read could silently answer from a directory the writer never
touched.  These tests point the two bindings at different directories and
require the record view's reads to follow the *writer's* directory -- resolved
on every call, fail-open, and without creating anything on disk.
"""

from __future__ import annotations

import json

import pytest

# ``pipeline.server`` must be imported before the store modules: a pre-existing
# import cycle requires it.
from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = "proj"
STORY = "s1"


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """Two empty directories: ``a`` for the reader's binding, ``b`` for writes."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", a)
    monkeypatch.setattr(persistence, "PLAN_DIR", b)
    return a, b


def _records():
    return FileStore().records()


def test_should_read_back_a_journal_record_when_server_and_persistence_point_at_different_directories(
    dirs,
):
    a, b = dirs
    records = _records()
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"ts": 1})

    written = b / f"{PLAN}.{STORY}.journal.json"
    assert not (a / f"{PLAN}.{STORY}.journal.json").exists()
    assert len(json.loads(written.read_text())) == 1

    entries = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert len(entries) == 1, f"read {len(entries)} journal records, expected 1"
    assert entries[0]["ts"] == 1


def test_should_read_back_a_decision_record_when_server_and_persistence_point_at_different_directories(
    dirs,
):
    a, _b = dirs
    records = _records()
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {"ts": 1})

    assert not (a / f"{PLAN}.decisions.json").exists()

    decisions = records.get_decisions(DEFAULT_NAMESPACE, PLAN)
    assert len(decisions) == 1, f"read {len(decisions)} decisions, expected 1"
    assert decisions[0]["ts"] == 1


def test_should_return_empty_for_a_malformed_journal_file_under_the_writer_directory(
    dirs,
):
    _, b = dirs
    (b / f"{PLAN}.{STORY}.journal.json").write_text("not json at all")

    assert _records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []


def test_should_return_empty_for_an_absent_journal(dirs):
    _, b = dirs

    assert _records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []
    assert not (b / f"{PLAN}.{STORY}.journal.json").exists()


def test_should_read_back_a_journal_record_with_split_directories(dirs, tmp_path):
    a, _b = dirs
    store = SqliteStore(str(tmp_path / "s.db"))
    try:
        records = store.records()
        records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"ts": 1})
        records.append_decision(DEFAULT_NAMESPACE, PLAN, {"ts": 2})

        assert not (a / f"{PLAN}.{STORY}.journal.json").exists()

        entries = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
        assert len(entries) == 1, f"read {len(entries)} journal records, expected 1"
        assert entries[0]["ts"] == 1

        decisions = records.get_decisions(DEFAULT_NAMESPACE, PLAN)
        assert len(decisions) == 1, f"read {len(decisions)} decisions, expected 1"
        assert decisions[0]["ts"] == 2
    finally:
        store.close()


def test_should_return_empty_for_a_non_list_journal_file_under_the_writer_directory(
    dirs,
):
    _, b = dirs
    path = b / f"{PLAN}.{STORY}.journal.json"
    path.write_text('{"not": "a list"}')

    records = _records()
    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []

    # ... and the empty answer was not cached.
    path.write_text('[{"ts": 5}]')
    entries = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert len(entries) == 1, f"read {len(entries)} journal records, expected 1"
    assert entries[0]["ts"] == 5


def test_should_re_resolve_the_writer_directory_on_every_call(dirs, tmp_path, monkeypatch):
    _a, _b = dirs
    records = _records()
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"ts": 1})
    assert [e["ts"] for e in records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)] == [1]

    c = tmp_path / "c"
    c.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", c)
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"ts": 2})

    entries = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert len(entries) == 1, f"read {len(entries)} journal records, expected 1"
    assert entries[0]["ts"] == 2


def test_reading_an_absent_journal_creates_no_file(dirs):
    a, b = dirs

    assert _records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []
    assert list(a.iterdir()) == []
    assert list(b.iterdir()) == []
