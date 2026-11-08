"""Journal activity on the record store contract (ADR 0012 stage B).

Two operations let stall detection and wedge recovery stop reasoning about
paths:

``get_journal_updated_at(namespace, plan_name, story_key)`` reports when the
story's journal was last appended to (epoch seconds), or ``None`` when the story
has no journal.  ``list_journal_keys(namespace, plan_name)`` reports the sorted
story keys that have a journal in that plan.

The behaviour is required of every implementation, so the contract cases below
run against all three (file, in-memory and sqlite) and the two that only one
implementation can show -- the in-memory clock high-water mark and the file
store's side-effect-free ``stat`` -- are exercised directly.
"""

from __future__ import annotations

import time

import pytest

# ``pipeline.server`` must be imported before the store modules: a pre-existing
# import cycle requires it.
from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

NAMESPACE = DEFAULT_NAMESPACE
OTHER_NAMESPACE = "acme"
PLAN = "p"
OTHER_PLAN = "q"
NESTED_PLAN = "p.x"
STORY = "s1"
INVALID_KEY = "../x"

# Which implementations serve exactly one namespace.  The others serve every
# valid namespace, so an unknown one is isolated rather than refused.
SINGLE_NAMESPACE = {"file": True, "memory": False, "sqlite": True}


@pytest.fixture(params=sorted(SINGLE_NAMESPACE))
def store_kind(request):
    """The implementation under test, by name."""
    return request.param


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """Point both plan-directory bindings at one empty directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return directory


@pytest.fixture()
def store(store_kind, plan_dir, tmp_path):
    """A record store of the kind under test, closed after the test."""
    if store_kind == "file":
        yield FileStore().records()
    elif store_kind == "memory":
        yield InMemoryRecordStore()
    else:
        legacy = SqliteStore(str(tmp_path / "s.db"))
        try:
            yield legacy.records()
        finally:
            legacy.close()


# -- get_journal_updated_at ------------------------------------------------
def test_should_report_no_updated_at_before_the_first_append(store):
    assert store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None


def test_should_report_an_updated_at_after_an_append(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    updated_at = store.get_journal_updated_at(NAMESPACE, PLAN, STORY)

    assert updated_at is not None
    assert updated_at > 0


def test_should_not_go_backwards_across_two_appends(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    first = store.get_journal_updated_at(NAMESPACE, PLAN, STORY)
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 2})
    second = store.get_journal_updated_at(NAMESPACE, PLAN, STORY)

    assert first is not None
    assert second is not None
    assert second >= first


def test_should_report_no_updated_at_after_clear_journal(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    assert store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is not None

    store.clear_journal(NAMESPACE, PLAN, STORY)

    assert store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None


def test_should_report_the_new_journal_after_a_clear_then_append(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    before = store.get_journal_updated_at(NAMESPACE, PLAN, STORY)
    store.clear_journal(NAMESPACE, PLAN, STORY)
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 2})

    after = store.get_journal_updated_at(NAMESPACE, PLAN, STORY)

    assert before is not None
    assert after is not None
    assert after >= before


def test_should_not_answer_another_plans_journal_activity(store):
    store.append_journal(NAMESPACE, OTHER_PLAN, STORY, {"ts": 1})

    assert store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None


def test_should_raise_on_an_invalid_story_key(store):
    with pytest.raises(InvalidRecordKeyError):
        store.get_journal_updated_at(NAMESPACE, PLAN, INVALID_KEY)


def test_should_raise_on_an_invalid_plan_name_when_reading_the_activity(store):
    with pytest.raises(InvalidRecordKeyError):
        store.get_journal_updated_at(NAMESPACE, INVALID_KEY, STORY)


# -- list_journal_keys -----------------------------------------------------
def test_should_list_no_journal_keys_for_an_unknown_plan(store):
    assert store.list_journal_keys(NAMESPACE, "no-such-plan") == []


def test_should_list_the_story_keys_that_have_a_journal(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    assert store.list_journal_keys(NAMESPACE, PLAN) == [STORY]


def test_should_list_journal_keys_in_sorted_order(store):
    for key in ("b", "a", "c"):
        store.append_journal(NAMESPACE, PLAN, key, {"ts": 1})

    assert store.list_journal_keys(NAMESPACE, PLAN) == ["a", "b", "c"]


def test_should_list_repeated_appends_to_one_journal_once(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 2})

    assert store.list_journal_keys(NAMESPACE, PLAN) == [STORY]


def test_should_not_list_another_plans_journal_keys(store):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    store.append_journal(NAMESPACE, OTHER_PLAN, "s2", {"ts": 1})

    assert store.list_journal_keys(NAMESPACE, PLAN) == [STORY]
    assert store.list_journal_keys(NAMESPACE, OTHER_PLAN) == ["s2"]


def test_should_not_leak_a_prefix_plans_journal_keys(store):
    # ``p`` is a prefix of ``p.x``, so the glob for ``p`` also matches
    # ``p.x.s1.journal.json``: the key it must not report is ``x.s1``.
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    store.append_journal(NAMESPACE, NESTED_PLAN, STORY, {"ts": 1})

    assert store.list_journal_keys(NAMESPACE, PLAN) == [STORY]
    assert store.list_journal_keys(NAMESPACE, NESTED_PLAN) == [STORY]


def test_should_not_list_a_cleared_journal(store):
    for key in ("a", "b", "c"):
        store.append_journal(NAMESPACE, PLAN, key, {"ts": 1})
    store.clear_journal(NAMESPACE, PLAN, "a")

    assert store.list_journal_keys(NAMESPACE, PLAN) == ["b", "c"]


def test_should_raise_on_an_invalid_plan_name_when_listing(store):
    with pytest.raises(InvalidRecordKeyError):
        store.list_journal_keys(NAMESPACE, INVALID_KEY)


def test_should_raise_on_an_invalid_namespace_when_listing(store):
    with pytest.raises(InvalidRecordKeyError):
        store.list_journal_keys("Bad NS", PLAN)


# -- namespaces ------------------------------------------------------------
def test_should_refuse_or_isolate_an_unknown_namespace_for_activity(
    store, store_kind
):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    if SINGLE_NAMESPACE[store_kind]:
        with pytest.raises(UnknownNamespaceError):
            store.get_journal_updated_at(OTHER_NAMESPACE, PLAN, STORY)
        return

    # A multi-namespace store isolates: the other namespace has no journal,
    # and writing there does not touch the default namespace.
    assert store.get_journal_updated_at(OTHER_NAMESPACE, PLAN, STORY) is None
    store.append_journal(OTHER_NAMESPACE, PLAN, STORY, {"ts": 9})
    assert store.get_journal_updated_at(OTHER_NAMESPACE, PLAN, STORY) is not None
    assert store.list_journal_keys(OTHER_NAMESPACE, PLAN) == [STORY]


def test_should_refuse_or_isolate_an_unknown_namespace_for_listing(
    store, store_kind
):
    store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    if SINGLE_NAMESPACE[store_kind]:
        with pytest.raises(UnknownNamespaceError):
            store.list_journal_keys(OTHER_NAMESPACE, PLAN)
        return

    assert store.list_journal_keys(OTHER_NAMESPACE, PLAN) == []
    store.append_journal(OTHER_NAMESPACE, PLAN, "s2", {"ts": 9})
    assert store.list_journal_keys(NAMESPACE, PLAN) == [STORY]


# -- the in-memory clock ---------------------------------------------------
@pytest.fixture()
def memory_store():
    return InMemoryRecordStore()


@pytest.fixture()
def clock(monkeypatch):
    """Control ``time.time`` for the in-memory store, and hand back the dial."""
    now = {"value": 100.0}
    monkeypatch.setattr(time, "time", lambda: now["value"])
    return now


def test_should_keep_the_high_water_mark_when_the_clock_goes_backwards(
    memory_store, clock
):
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 100.0

    clock["value"] = 50.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 2})
    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 100.0

    clock["value"] = 60.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 3})
    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 100.0

    clock["value"] = 150.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 4})
    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 150.0


def test_should_capture_the_time_of_the_append_in_memory(memory_store, clock):
    clock["value"] = 200.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 200.0


def test_should_forget_the_high_water_mark_when_the_journal_is_cleared(
    memory_store, clock
):
    clock["value"] = 150.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    memory_store.clear_journal(NAMESPACE, PLAN, STORY)
    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None

    # A cleared journal is a new journal, so an earlier clock time is its own
    # time -- not the stale high-water mark of the journal that was deleted.
    clock["value"] = 120.0
    memory_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 2})

    assert memory_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) == 120.0


# -- the file store's stat -------------------------------------------------
@pytest.fixture()
def file_store(plan_dir):
    return FileStore().records()


def test_should_read_the_journal_time_without_creating_the_file(
    file_store, plan_dir
):
    journal = plan_dir / f"{PLAN}.{STORY}.journal.json"

    assert file_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None
    assert not journal.exists(), "reading the activity must not create the file"

    file_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})
    updated_at = file_store.get_journal_updated_at(NAMESPACE, PLAN, STORY)

    assert isinstance(updated_at, float)
    assert updated_at == journal.stat().st_mtime

    file_store.clear_journal(NAMESPACE, PLAN, STORY)

    assert not journal.exists()
    assert file_store.get_journal_updated_at(NAMESPACE, PLAN, STORY) is None


def test_should_read_the_activity_from_the_directory_the_writer_appends_to(
    tmp_path, monkeypatch
):
    reader_dir = tmp_path / "reader"
    writer_dir = tmp_path / "writer"
    reader_dir.mkdir()
    writer_dir.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", reader_dir)
    monkeypatch.setattr(persistence, "PLAN_DIR", writer_dir)
    records = FileStore().records()

    records.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    assert (writer_dir / f"{PLAN}.{STORY}.journal.json").exists()
    updated_at = records.get_journal_updated_at(NAMESPACE, PLAN, STORY)
    assert updated_at == (writer_dir / f"{PLAN}.{STORY}.journal.json").stat().st_mtime
    assert records.list_journal_keys(NAMESPACE, PLAN) == [STORY]
    assert not (reader_dir / f"{PLAN}.{STORY}.journal.json").exists()


def test_should_ignore_a_journal_file_that_is_not_a_story_key(
    file_store, plan_dir
):
    # A hand-placed file whose middle segment is not a valid key is skipped
    # rather than reported (a path fragment can never be a story key).
    (plan_dir / f"{PLAN}.journal.json").write_text("[]")
    file_store.append_journal(NAMESPACE, PLAN, STORY, {"ts": 1})

    assert file_store.list_journal_keys(NAMESPACE, PLAN) == [STORY]
