"""The sqlite store keeps its decisions, journals, drafts and snapshots in the
database (ADR 0012 stage C).

Decisions and journals used to be file sidecars under ``PLAN_DIR`` that the
sqlite store delegated to its internal file store.  They are now tables of the
store's own database -- ``decisions`` and ``journal`` -- written through
parameterized statements only.  The plan draft, the plan-completion marker and
report and the content-addressed context snapshot move into tables as well, in
the migration ordered right after the one that moved the decisions and
journals.

What is graded here is observable behaviour:

* the records are rows in the database, not files in the plans directory, so
  the plans directory stays empty;
* a record written through one API is read back through the other;
* an append keeps its order, an append made inside an open transaction commits
  with it, and a refused key writes nothing at all;
* the journal read still drops the display-only fields the writer never set;
* the completion marker is created exactly once, even under two racing callers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import store_sqlite_schema
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
OTHER_PLAN = 'plan-b'
STORY = 'STORY-1'
OTHER_STORY = 'STORY-2'

DRAFT = {'epics': [{'summary': 'e', 'stories': [{'key': 'S-1'}]}]}
REPORT = 'SUMMARY[plan-a]\n'
SNAPSHOT_CONTENT = '--- guide.md ---\nUSE-TABS-ALWAYS\n'
SNAPSHOT_META = {'provider': 'dir', 'uri': '/srv/docs'}


def _digest_of(content: str) -> str:
    """Return the canonical digest of *content*."""
    return 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()


# -- fixtures -----------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    """The absolute path of the database under test."""
    return str(tmp_path / 'store.db')


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory wired into every module-level binding.

    The directory starts empty and must stay empty: nothing these operations
    write may land on disk.
    """
    directory = tmp_path / 'plans'
    directory.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', directory)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', directory)
    return directory


@pytest.fixture()
def store(db_path, plan_dir):
    """A ``SqliteStore`` on a fresh database, closed after the test."""
    handle = SqliteStore(db_path)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def records(store):
    """The namespaced record view of the store under test."""
    return store.records()


# -- helpers ------------------------------------------------------------


def _plan_dir_entries(plan_dir: Path) -> list[str]:
    """Every path under the plans directory, relative and sorted."""
    return sorted(str(path.relative_to(plan_dir)) for path in plan_dir.rglob('*'))


def _tables(db_path: str) -> set[str]:
    """The table names the database holds."""
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def _columns(db_path: str, table: str) -> list[str]:
    """The column names of *table*, in declaration order."""
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]


def _rows(db_path: str, table: str) -> list[dict]:
    """Every row of *table* as a mapping, in rowid order.

    A table the database does not hold has no rows, so a test that asserts
    "nothing was written" reads the same before and after the table exists.
    """
    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
        if not columns:
            return []
        records = conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
    return [dict(zip(columns, row)) for row in records]


def _assert_table_is_empty(db_path: str, table: str) -> None:
    """Assert *table* is part of the schema and holds no row."""
    assert table in _tables(db_path), f'the database must hold a {table} table'
    assert _rows(db_path, table) == [], f'no row may be written to {table}'


class _NoFileStore:
    """A stand-in for the internal file store that refuses to serve records.

    Any attribute a caller reaches for is refused, so a store that still
    delegated a record operation to its file store would fail loudly here
    instead of quietly writing a file.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f'the sqlite store must not delegate {name!r} to its file store'
        )


# -- the schema ---------------------------------------------------------


def test_the_decisions_table_holds_the_documented_columns(db_path, plan_dir):
    """The ``decisions`` table is keyed by namespace, plan and sequence."""
    handle = SqliteStore(db_path)
    handle.close()

    assert 'decisions' in _tables(db_path)
    columns = _columns(db_path, 'decisions')
    for name in ('namespace', 'plan', 'seq', 'record_json'):
        assert name in columns, f'the decisions table must have a {name} column'


def test_the_journal_table_holds_the_documented_columns(db_path, plan_dir):
    """The ``journal`` table is keyed by namespace, plan, story and sequence."""
    handle = SqliteStore(db_path)
    handle.close()

    assert 'journal' in _tables(db_path)
    columns = _columns(db_path, 'journal')
    for name in ('namespace', 'plan', 'story_key', 'seq', 'record_json'):
        assert name in columns, f'the journal table must have a {name} column'


def test_the_migrations_are_ordered_and_the_store_is_at_the_latest(db_path):
    """The decisions and the journal move in one migration, the rest in the next.

    The migrations are appended, never renumbered, so the decisions-and-journal
    migration comes before the one that moved the drafts, the completion
    records and the snapshots.
    """
    assert store_sqlite_schema.latest_version() >= 3
    assert len(store_sqlite_schema.MIGRATIONS) >= 3

    handle = SqliteStore(db_path)
    handle.close()

    with sqlite3.connect(db_path) as conn:
        version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
    assert version == store_sqlite_schema.latest_version()


# -- decisions ----------------------------------------------------------


def test_two_appended_decisions_keep_their_order(records):
    """The decisions of a plan read back in the order they were written."""
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'second'})

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [
        {'decision': 'first'},
        {'decision': 'second'},
    ]


def test_the_decisions_of_two_plans_stay_separate(records):
    """A decision of one plan is not served for another."""
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'mine'})
    records.append_decision(DEFAULT_NAMESPACE, OTHER_PLAN, {'decision': 'other'})

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [{'decision': 'mine'}]
    assert records.get_decisions(DEFAULT_NAMESPACE, OTHER_PLAN) == [
        {'decision': 'other'}
    ]


def test_the_decisions_of_an_unknown_plan_are_empty(records):
    """A plan with no decisions has an empty history, not an error."""
    assert records.get_decisions(DEFAULT_NAMESPACE, 'no-such-plan') == []


def test_two_appended_decisions_are_two_rows_with_rising_sequences(db_path, records):
    """Each decision is its own row, and the sequence numbers rise with them."""
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'second'})

    rows = _rows(db_path, 'decisions')
    assert len(rows) == 2
    assert [row['namespace'] for row in rows] == [DEFAULT_NAMESPACE, DEFAULT_NAMESPACE]
    assert [row['plan'] for row in rows] == [PLAN, PLAN]
    assert [json.loads(row['record_json']) for row in rows] == [
        {'decision': 'first'},
        {'decision': 'second'},
    ]
    assert rows[0]['seq'] < rows[1]['seq'], 'the sequence must rise with the appends'


def test_a_decision_is_not_written_under_the_plans_directory(records, plan_dir):
    """The decision lands in the database, so the plans directory stays empty."""
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})

    assert _plan_dir_entries(plan_dir) == []


def test_a_decision_written_through_the_legacy_api_is_read_by_the_record_view(
    store, records
):
    """The legacy API and the record view are two views of one database."""
    store.append_decision(PLAN, {'decision': 'legacy'})

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [{'decision': 'legacy'}]


def test_a_decision_written_through_the_record_view_is_read_by_the_legacy_api(
    store, records
):
    """A decision written through the record view is served by the legacy API."""
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'records'})

    assert store.get_decisions(PLAN) == [{'decision': 'records'}]


def test_the_legacy_decision_api_writes_nothing_under_the_plans_directory(
    store, plan_dir
):
    """The legacy append writes to the database, not to a decisions sidecar."""
    store.append_decision(PLAN, {'decision': 'legacy'})

    assert _plan_dir_entries(plan_dir) == []


def test_the_decisions_do_not_go_through_the_internal_file_store(store, records):
    """The decisions are the database's, not the file store's."""
    store._files = _NoFileStore()

    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})
    records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'second'})

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [
        {'decision': 'first'},
        {'decision': 'second'},
    ]


def test_a_refused_decision_key_writes_nothing(db_path, records):
    """An invalid plan name is refused before a row is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.append_decision(DEFAULT_NAMESPACE, '../x', {'decision': 'first'})

    _assert_table_is_empty(db_path, 'decisions')
    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == []


def test_a_decision_that_is_not_a_dict_is_refused(db_path, records):
    """A record that is not a dict is refused and writes nothing."""
    with pytest.raises(TypeError):
        records.append_decision(DEFAULT_NAMESPACE, PLAN, 'not a dict')

    _assert_table_is_empty(db_path, 'decisions')


def test_an_unknown_namespace_is_refused_for_a_decision(db_path, records):
    """Only the default namespace is served, and the refusal writes nothing."""
    with pytest.raises(UnknownNamespaceError):
        records.append_decision('ops', PLAN, {'decision': 'first'})

    _assert_table_is_empty(db_path, 'decisions')


def test_a_decision_record_is_stored_as_it_was_written(records):
    """The record reads back exactly as it was handed over."""
    record = {'decision': 'first', 'why': 'because', 'n': 3}
    records.append_decision(DEFAULT_NAMESPACE, PLAN, record)

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [record]


def test_a_decision_record_holding_a_quote_round_trips(records):
    """A record is stored as data, so a quote in it is not a statement.

    A statement built by interpolation would break on this record; a
    parameterized one hands it back untouched.
    """
    record = {'decision': "'; DROP TABLE decisions; --"}
    records.append_decision(DEFAULT_NAMESPACE, PLAN, record)

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [record]


# -- journals -----------------------------------------------------------


def test_two_appended_journal_records_keep_their_order(records):
    """The journal of a story reads back in the order it was written."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 2})

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        {'entry': 1},
        {'entry': 2},
    ]


def test_the_journals_of_two_stories_stay_separate(records):
    """A journal entry of one story is not served for another."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})
    records.append_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY, {'entry': 2})

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [{'entry': 1}]
    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY) == [
        {'entry': 2}
    ]


def test_the_journal_of_an_unknown_story_is_empty(records):
    """A story with no journal has an empty history, not an error."""
    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []


def test_two_appended_journal_records_are_two_rows_with_rising_sequences(
    db_path, records
):
    """Each journal entry is its own row, and the sequences rise with them."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 2})

    rows = _rows(db_path, 'journal')
    assert len(rows) == 2
    assert [row['namespace'] for row in rows] == [DEFAULT_NAMESPACE, DEFAULT_NAMESPACE]
    assert [row['plan'] for row in rows] == [PLAN, PLAN]
    assert [row['story_key'] for row in rows] == [STORY, STORY]
    assert [json.loads(row['record_json']) for row in rows] == [
        {'entry': 1},
        {'entry': 2},
    ]
    assert rows[0]['seq'] < rows[1]['seq'], 'the sequence must rise with the appends'


def test_a_journal_record_is_not_written_under_the_plans_directory(
    records, plan_dir
):
    """The journal lands in the database, so the plans directory stays empty."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})

    assert _plan_dir_entries(plan_dir) == []


def test_the_journal_omits_the_display_only_fields_the_writer_never_set(records):
    """A record read back is the record that was written.

    The file store decorated every entry with the display-only fields
    (``step``, ``summary``, ``next_hint``) present and ``None`` when the
    writer gave none; the record view omits the fields a record never had.
    """
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})

    entry = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)[0]
    assert entry == {'entry': 1}
    for name in ('step', 'summary', 'next_hint'):
        assert name not in entry, f'{name} must be omitted when it was never set'


def test_a_journal_record_that_sets_a_display_field_keeps_it(records):
    """A display-only field the writer did set is served as it was set."""
    records.append_journal(
        DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1, 'step': 's1', 'summary': 'ok'}
    )

    entry = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)[0]
    assert entry['step'] == 's1'
    assert entry['summary'] == 'ok'


def test_a_journal_written_through_the_legacy_api_is_read_by_the_record_view(
    store, records
):
    """The legacy API and the record view are two views of one database."""
    store.append_journal(PLAN, STORY, {'entry': 1})

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [{'entry': 1}]


def test_the_records_survive_a_reopen_with_another_plans_directory(
    db_path, plan_dir, tmp_path, monkeypatch
):
    """The records are the database's, so a fresh store reads them back.

    The reopened store is pointed at a different, empty plans directory: a
    record that lived in a file would be gone, one that lives in the database
    is served.
    """
    handle = SqliteStore(db_path)
    try:
        view = handle.records()
        view.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})
        view.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})
        view.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
        view.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)
        view.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)
        digest = _digest_of(SNAPSHOT_CONTENT)
        view.put_context_snapshot(
            DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, SNAPSHOT_META
        )
    finally:
        handle.close()

    elsewhere = tmp_path / 'other-plans'
    elsewhere.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', elsewhere)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', elsewhere)

    reopened = SqliteStore(db_path)
    try:
        view = reopened.records()
        assert view.get_decisions(DEFAULT_NAMESPACE, PLAN) == [{'decision': 'first'}]
        assert view.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [{'entry': 1}]
        assert view.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT
        assert view.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
        assert view.get_plan_report(DEFAULT_NAMESPACE, PLAN) == REPORT
        stored = view.get_context_snapshot(DEFAULT_NAMESPACE, digest)
        assert stored is not None
        assert stored['content'] == SNAPSHOT_CONTENT
    finally:
        reopened.close()


def test_a_journal_written_through_the_record_view_is_read_by_the_legacy_api(
    store, records
):
    """A journal written through the record view is served by the legacy API."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})

    available, entries = store.get_journal(PLAN, STORY)
    assert available is True
    assert len(entries) == 1
    assert entries[0]['entry'] == 1


def test_the_legacy_journal_api_writes_nothing_under_the_plans_directory(
    store, plan_dir
):
    """The legacy append writes to the database, not to a journal sidecar."""
    store.append_journal(PLAN, STORY, {'entry': 1})

    assert _plan_dir_entries(plan_dir) == []


def test_the_journal_does_not_go_through_the_internal_file_store(store, records):
    """The journal is the database's, not the file store's."""
    store._files = _NoFileStore()

    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 2})

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        {'entry': 1},
        {'entry': 2},
    ]


def test_a_refused_journal_key_writes_nothing(db_path, records):
    """An invalid plan name is refused before a row is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.append_journal(DEFAULT_NAMESPACE, '../x', STORY, {'entry': 1})

    _assert_table_is_empty(db_path, 'journal')


def test_a_refused_journal_story_key_writes_nothing(db_path, records):
    """An invalid story key is refused before a row is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.append_journal(DEFAULT_NAMESPACE, PLAN, '../x', {'entry': 1})

    _assert_table_is_empty(db_path, 'journal')


def test_a_journal_record_that_is_not_a_dict_is_refused(db_path, records):
    """A record that is not a dict is refused and writes nothing."""
    with pytest.raises(TypeError):
        records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, 'not a dict')

    _assert_table_is_empty(db_path, 'journal')


def test_an_unknown_namespace_is_refused_for_a_journal(db_path, records):
    """Only the default namespace is served, and the refusal writes nothing."""
    with pytest.raises(UnknownNamespaceError):
        records.append_journal('ops', PLAN, STORY, {'entry': 1})

    _assert_table_is_empty(db_path, 'journal')


# -- the transaction ----------------------------------------------------


def test_a_journal_appended_inside_a_transaction_commits_with_it(store, records):
    """An append made inside an open transaction is durable when it commits."""
    with store.transaction(PLAN) as acquired:
        assert acquired is True
        records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 1})

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [{'entry': 1}]


def test_a_journal_appended_inside_a_failing_transaction_is_rolled_back(
    store, records
):
    """An append made inside a transaction that raises leaves no row behind."""
    records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 'kept'})

    with pytest.raises(RuntimeError), store.transaction(PLAN) as acquired:
        assert acquired is True
        records.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'entry': 'dropped'})
        raise RuntimeError('boom')

    assert records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        {'entry': 'kept'}
    ]


def test_a_decision_appended_inside_a_transaction_commits_with_it(store, records):
    """An append made inside an open transaction is durable when it commits."""
    with store.transaction(PLAN) as acquired:
        assert acquired is True
        records.append_decision(DEFAULT_NAMESPACE, PLAN, {'decision': 'first'})

    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN) == [{'decision': 'first'}]


# -- the plan draft -----------------------------------------------------


def test_a_plan_draft_round_trips_through_the_database(records):
    """The draft is stored and read back, exactly as it was handed over."""
    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert records.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT


def test_a_plan_draft_is_not_written_under_the_plans_directory(records, plan_dir):
    """The draft lands in the database, so the plans directory stays empty."""
    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert _plan_dir_entries(plan_dir) == []


def test_a_second_plan_draft_replaces_the_first(records):
    """A draft is replaced wholesale, never merged."""
    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, {'epics': [], 'stale': True})
    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, {'epics': [{'summary': 'new'}]})

    stored = records.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
    assert stored == {'epics': [{'summary': 'new'}]}
    assert 'stale' not in stored


def test_a_deleted_plan_draft_is_gone(records):
    """Deleting a draft reports that it deleted one, and the second does not."""
    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert records.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is True
    assert records.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None
    assert records.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is False


def test_the_plan_drafts_are_listed_in_sorted_order(records):
    """The plans that have a draft are listed, sorted by name."""
    records.put_plan_draft(DEFAULT_NAMESPACE, 'zeta', DRAFT)
    records.put_plan_draft(DEFAULT_NAMESPACE, 'alpha', DRAFT)

    assert records.list_plan_drafts(DEFAULT_NAMESPACE) == ['alpha', 'zeta']


def test_a_refused_plan_draft_key_writes_nothing(records, plan_dir):
    """An invalid plan name is refused before anything is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.put_plan_draft(DEFAULT_NAMESPACE, '../x', DRAFT)

    assert _plan_dir_entries(plan_dir) == []


def test_a_plan_draft_does_not_go_through_the_internal_file_store(store, records):
    """The draft is the database's, not the file store's."""
    store._files = _NoFileStore()

    records.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert records.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT


# -- the plan completion records ----------------------------------------


def test_the_plan_completion_marker_is_created_once(records):
    """The marker is once-only: the first mark creates it, the second does not."""
    assert records.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
    assert records.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is False
    assert records.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True


def test_the_plan_completion_marker_is_not_written_under_the_plans_directory(
    records, plan_dir
):
    """The marker lands in the database, so the plans directory stays empty."""
    records.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)

    assert _plan_dir_entries(plan_dir) == []


def test_a_plan_report_round_trips_through_the_database(records):
    """The report is stored and read back, exactly as it was handed over."""
    records.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)

    assert records.get_plan_report(DEFAULT_NAMESPACE, PLAN) == REPORT


def test_a_second_plan_report_replaces_the_first(records):
    """A report is replaced wholesale, never appended to."""
    records.put_plan_report(DEFAULT_NAMESPACE, PLAN, 'first')
    records.put_plan_report(DEFAULT_NAMESPACE, PLAN, 'second')

    assert records.get_plan_report(DEFAULT_NAMESPACE, PLAN) == 'second'


def test_the_plan_report_is_not_written_under_the_plans_directory(
    records, plan_dir
):
    """The report lands in the database, so the plans directory stays empty."""
    records.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)

    assert _plan_dir_entries(plan_dir) == []


def test_a_refused_plan_completion_key_writes_nothing(records, plan_dir):
    """An invalid plan name is refused before anything is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.mark_plan_completed(DEFAULT_NAMESPACE, '../x')

    assert _plan_dir_entries(plan_dir) == []


def test_the_plan_completion_records_do_not_go_through_the_file_store(
    store, records
):
    """The marker and the report are the database's, not the file store's."""
    store._files = _NoFileStore()

    records.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)
    records.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)

    assert records.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
    assert records.get_plan_report(DEFAULT_NAMESPACE, PLAN) == REPORT


def test_mark_plan_completed_is_atomic_under_two_concurrent_callers(db_path):
    """Two racing callers create the marker once: exactly one of them wins."""
    plan_dir = db_path + '.plans'
    Path(plan_dir).mkdir()
    original_server_dir = pipeline.server.PLAN_DIR
    original_persistence_dir = pipeline.persistence.PLAN_DIR
    pipeline.server.PLAN_DIR = Path(plan_dir)
    pipeline.persistence.PLAN_DIR = Path(plan_dir)
    try:
        handle = SqliteStore(db_path)
        try:
            view = handle.records()
            results: list[bool] = []
            barrier = threading.Barrier(2)

            def mark():
                barrier.wait()
                results.append(view.mark_plan_completed(DEFAULT_NAMESPACE, PLAN))

            threads = [threading.Thread(target=mark) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert sorted(results) == [False, True]
        finally:
            handle.close()
    finally:
        pipeline.server.PLAN_DIR = original_server_dir
        pipeline.persistence.PLAN_DIR = original_persistence_dir


# -- the context snapshot -----------------------------------------------


def test_a_context_snapshot_round_trips_through_the_database(records):
    """The snapshot is stored and read back, with its content and its meta."""
    digest = _digest_of(SNAPSHOT_CONTENT)
    records.put_context_snapshot(
        DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, SNAPSHOT_META
    )

    stored = records.get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert stored is not None
    assert stored['content'] == SNAPSHOT_CONTENT
    assert stored['meta']['provider'] == SNAPSHOT_META['provider']
    assert stored['meta']['uri'] == SNAPSHOT_META['uri']


def test_a_context_snapshot_is_not_written_under_the_plans_directory(
    records, plan_dir
):
    """The snapshot lands in the database, so the plans directory stays empty."""
    digest = _digest_of(SNAPSHOT_CONTENT)
    records.put_context_snapshot(
        DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, SNAPSHOT_META
    )

    assert _plan_dir_entries(plan_dir) == []


def test_a_second_snapshot_of_the_same_digest_keeps_the_first(records):
    """A snapshot is written once: a later put never overwrites it.

    The row is keyed by the digest and inserted-or-ignored, so the second put
    of the same content keeps the meta the first put stored.
    """
    digest = _digest_of(SNAPSHOT_CONTENT)
    records.put_context_snapshot(
        DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, {'provider': 'first'}
    )
    records.put_context_snapshot(
        DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, {'provider': 'second'}
    )

    stored = records.get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert stored is not None
    assert stored['meta']['provider'] == 'first'


def test_an_absent_context_snapshot_reads_back_as_none(records):
    """A digest that was never stored has no snapshot."""
    assert records.get_context_snapshot(DEFAULT_NAMESPACE, _digest_of('absent')) is None


def test_a_context_snapshot_whose_digest_does_not_match_is_refused(
    records, plan_dir
):
    """A digest that addresses other content is refused, and nothing is written."""
    with pytest.raises(ValueError):
        records.put_context_snapshot(
            DEFAULT_NAMESPACE, _digest_of('other'), SNAPSHOT_CONTENT, SNAPSHOT_META
        )

    assert _plan_dir_entries(plan_dir) == []


def test_a_malformed_context_digest_is_refused(records, plan_dir):
    """A digest that is not a digest is refused, and nothing is written."""
    with pytest.raises(InvalidRecordKeyError):
        records.put_context_snapshot(
            DEFAULT_NAMESPACE, 'not-a-digest', SNAPSHOT_CONTENT, SNAPSHOT_META
        )

    assert _plan_dir_entries(plan_dir) == []


def test_a_context_snapshot_does_not_go_through_the_internal_file_store(
    store, records
):
    """The snapshot is the database's, not the file store's."""
    store._files = _NoFileStore()
    digest = _digest_of(SNAPSHOT_CONTENT)

    records.put_context_snapshot(
        DEFAULT_NAMESPACE, digest, SNAPSHOT_CONTENT, SNAPSHOT_META
    )

    stored = records.get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert stored is not None
    assert stored['content'] == SNAPSHOT_CONTENT


def test_an_unknown_namespace_is_refused_for_a_context_snapshot(records):
    """Only the default namespace is served."""
    with pytest.raises(UnknownNamespaceError):
        records.get_context_snapshot('ops', _digest_of(SNAPSHOT_CONTENT))
