"""The sqlite store's own outbox table (ADR 0012 decision 9).

The outbox queues of a ``PIPELINE_STORE=sqlite`` deployment live in the
database: a row per queued record, in the ``outbox`` table.  These tests
exercise the behaviour through the public record-store API
(``append_outbox`` / ``read_outbox`` / ``ack_outbox`` / ``list_outboxes`` /
``outbox_summary``) and look at the database file itself only to pin the
storage the story specifies: the table, its columns and its index.

The queue is a record, not a file, so a spool file left behind by an earlier
file-store deployment is not imported: the operator drains the old queues
before switching stores.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import notification_outbox, paths
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
OTHER_PLAN = 'plan-b'
INSTANCE = 'email-a'
LEGACY = '_legacy'


@pytest.fixture()
def db_path(tmp_path):
    """The absolute database path every store in one test shares."""
    return str(tmp_path / 's.db')


@pytest.fixture()
def plan_dir(db_path, monkeypatch):
    """Isolate ``PLAN_DIR`` exactly as ``test_store_sqlite`` does."""
    directory = db_path + '.plans'
    Path(directory).mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', Path(directory))
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', Path(directory))
    monkeypatch.setattr(paths, 'PLAN_DIR', Path(directory))
    monkeypatch.setattr(notification_outbox, 'PLAN_DIR', Path(directory))
    return Path(directory)


@pytest.fixture()
def records(db_path, plan_dir):
    """A ``records()`` view on a fresh database, closed after the test."""
    store = SqliteStore(db_path)
    try:
        yield store.records()
    finally:
        store.close()


def _rows(db_path: str, sql: str) -> list[sqlite3.Row]:
    """Run ``sql`` against the database file, out of band from the store."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _outbox_rows(db_path: str) -> list[sqlite3.Row]:
    """Every row of the ``outbox`` table."""
    return _rows(db_path, 'SELECT * FROM outbox')


def _table_columns(db_path: str, table: str) -> dict[str, sqlite3.Row]:
    """The ``PRAGMA table_info`` rows of ``table``, keyed by column name."""
    return {row['name']: row for row in _rows(db_path, f'PRAGMA table_info({table})')}


# -- the table ------------------------------------------------------------


def test_the_outbox_table_exists_with_the_documented_columns(db_path, records):
    """The queue is a table with the columns the story names."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    columns = _table_columns(db_path, 'outbox')
    assert set(columns) == {'id', 'namespace', 'plan', 'channel', 'ts', 'record'}
    assert columns['id']['type'].upper() == 'INTEGER'
    assert columns['id']['pk'] == 1
    for required in ('namespace', 'plan', 'channel', 'record'):
        assert columns[required]['notnull'] == 1, f'{required} must be NOT NULL'
    assert columns['ts']['notnull'] == 0, 'ts is NULL when the record has none'


def test_the_outbox_id_is_an_autoincrementing_integer(db_path, records):
    """``id`` is an ``INTEGER PRIMARY KEY AUTOINCREMENT`` column."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'y'})
    schema = _rows(
        db_path, "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'"
    )
    assert schema, 'the outbox table must exist'
    assert 'autoincrement' in schema[0]['sql'].lower()
    ids = [row['id'] for row in _outbox_rows(db_path)]
    assert all(isinstance(value, int) for value in ids)
    assert ids[1] > ids[0]


def test_the_outbox_table_is_indexed_by_queue_and_id(db_path, records):
    """An index covers ``(namespace, plan, channel, id)``."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    indexes = _rows(db_path, "PRAGMA index_list('outbox')")
    assert indexes, 'the outbox table must carry an index'
    indexed = []
    for index in indexes:
        columns = [
            row['name'] for row in _rows(db_path, f"PRAGMA index_info('{index['name']}')")
        ]
        indexed.append(columns)
    assert any(
        columns[:4] == ['namespace', 'plan', 'channel', 'id'] for columns in indexed
    ), f'no index on (namespace, plan, channel, id): {indexed}'


def test_a_queued_record_is_a_row_in_the_outbox_table(db_path, records):
    """The row carries the queue it belongs to and the record itself."""
    record = {'ts': '2024-05-05T10:00:00+00:00', 'message': 'hello'}
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, record)
    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row['namespace'] == DEFAULT_NAMESPACE
    assert row['plan'] == PLAN
    assert row['channel'] == INSTANCE
    assert row['ts'] == '2024-05-05T10:00:00+00:00'
    assert json.loads(row['record']) == record


def test_the_ts_column_is_null_when_the_record_has_no_string_ts(db_path, records):
    """``ts`` is the record's top-level ts when it is a string, else NULL."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'message': 'no ts'})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': 12345})
    rows = _outbox_rows(db_path)
    assert len(rows) == 2
    assert all(row['ts'] is None for row in rows)


def test_the_summary_reads_only_the_timestamp_column(db_path, records):
    """The summary counts rows; it never carries the payload."""
    records.append_outbox(
        DEFAULT_NAMESPACE,
        PLAN,
        INSTANCE,
        {'ts': '2024-05-05T10:00:00+00:00', 'message': 'top-secret-payload'},
    )
    summary = records.outbox_summary(DEFAULT_NAMESPACE)
    assert summary['records'] == 1
    assert 'top-secret-payload' not in repr(summary)


# -- the queue is a record, not a file ------------------------------------


def test_a_queued_record_is_not_written_to_a_spool_file(records, plan_dir):
    """The queue never reaches the filesystem."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'event': 'y'})
    assert list(plan_dir.glob('*.outbox*.jsonl')) == []


def test_a_spool_file_on_disk_is_not_imported(records, plan_dir):
    """An existing file-store spool is not served and not counted."""
    (plan_dir / f'{PLAN}.outbox.jsonl').write_text(
        json.dumps({'ts': '2024-05-05T10:00:00+00:00', 'message': 'old'}) + '\n',
        encoding='utf-8',
    )
    (plan_dir / f'{PLAN}.outbox.{INSTANCE}.jsonl').write_text(
        json.dumps({'ts': '2024-05-05T10:00:00+00:00', 'message': 'old'}) + '\n',
        encoding='utf-8',
    )
    assert records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []
    assert records.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY) == []
    assert records.list_outboxes(DEFAULT_NAMESPACE) == []
    summary = records.outbox_summary(DEFAULT_NAMESPACE)
    assert summary['records'] == 0
    assert summary['oldest_age_s'] is None


def test_a_queued_record_is_visible_to_another_store_on_the_same_file(
    db_path, plan_dir, records
):
    """A second store on the same database sees the row at once."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    other = SqliteStore(db_path)
    try:
        entries = other.records().read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    finally:
        other.close()
    assert len(entries) == 1
    assert entries[0]['record'] == {'event': 'x'}


def test_an_entry_id_is_the_string_form_of_its_row_id(db_path, records):
    """The id handed to a caller is ``str(row id)``."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    rows = _outbox_rows(db_path)
    assert len(entries) == len(rows) == 1
    assert entries[0]['id'] == str(rows[0]['id'])
    assert isinstance(entries[0]['id'], str)


# -- refusals write nothing ----------------------------------------------


def test_an_invalid_channel_is_refused_before_any_row_is_written(db_path, records):
    """A bad channel raises and leaves the table empty."""
    with pytest.raises(InvalidRecordKeyError) as excinfo:
        records.append_outbox(DEFAULT_NAMESPACE, PLAN, 'Bad Channel', {'n': 0})
    assert 'channel' in str(excinfo.value).lower()
    assert _outbox_rows(db_path) == []


def test_an_unknown_namespace_is_refused_before_any_row_is_written(db_path, records):
    """Another namespace raises and leaves the table empty."""
    with pytest.raises(UnknownNamespaceError):
        records.append_outbox('acme', PLAN, INSTANCE, {'n': 0})
    assert _outbox_rows(db_path) == []


def test_a_record_that_is_not_a_dict_is_refused_before_any_row_is_written(
    db_path, records
):
    """A non-dict record raises and leaves the table empty."""
    with pytest.raises(TypeError):
        records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, 'not a dict')
    assert _outbox_rows(db_path) == []


def test_a_record_carrying_sql_is_stored_as_data(db_path, records):
    """A record that quotes SQL is stored as text; the table survives."""
    payload = "'); DROP TABLE outbox; --"
    record = {'message': payload, 'nested': {'sql': payload}}
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, record)
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 1
    assert entries[0]['record'] == record
    rows = _outbox_rows(db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]['record']) == record


def test_an_outbox_operation_opens_no_second_connection(db_path, records, monkeypatch):
    """The row operations run on the store's own connection."""
    opened: list = []
    original = sqlite3.connect

    def counting_connect(*args, **kwargs):
        opened.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', counting_connect)
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    records.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']])
    records.outbox_summary(DEFAULT_NAMESPACE)
    assert opened == [], 'the store must not open a second connection'


def test_an_existing_outbox_table_is_kept(db_path):
    """Opening the store over an existing outbox table keeps its rows."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'CREATE TABLE outbox ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'namespace TEXT NOT NULL, '
            'plan TEXT NOT NULL, '
            'channel TEXT NOT NULL, '
            'ts TEXT, '
            'record TEXT NOT NULL)'
        )
        conn.execute(
            'INSERT INTO outbox (namespace, plan, channel, ts, record) '
            "VALUES ('default', 'plan-a', 'email-a', NULL, '{}')"
        )
        conn.commit()
    finally:
        conn.close()
    store = SqliteStore(db_path)
    try:
        assert len(_outbox_rows(db_path)) == 1
    finally:
        store.close()


def test_concurrent_appends_all_land(records):
    """The row operations are serialised, so no append is lost."""
    def worker() -> None:
        for index in range(5):
            records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': index})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 20


def test_two_queued_records_have_different_ids(records):
    """Every row carries its own id."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 2
    assert entries[0]['id'] != entries[1]['id']


# -- the summary ----------------------------------------------------------


def test_the_summary_counts_the_queued_records(records):
    """The summary reports how many records are queued."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
    records.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'n': 2})
    summary = records.outbox_summary(DEFAULT_NAMESPACE)
    assert set(summary) == {'files', 'records', 'oldest_age_s'}
    assert summary['records'] == 3
    assert isinstance(summary['files'], int)
    assert summary['files'] >= 0


def test_the_summary_of_one_plan_counts_only_that_plan(records):
    """A plan's summary leaves the other plans out."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    records.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'n': 1})
    assert records.outbox_summary(DEFAULT_NAMESPACE, PLAN)['records'] == 1
    assert records.outbox_summary(DEFAULT_NAMESPACE, OTHER_PLAN)['records'] == 1
    assert records.outbox_summary(DEFAULT_NAMESPACE)['records'] == 2


def test_the_summary_reports_the_age_of_the_oldest_record(records):
    """The age comes from the record's timestamp, in seconds."""
    old = datetime.now(UTC) - timedelta(hours=1)
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': old.isoformat()})
    age = records.outbox_summary(DEFAULT_NAMESPACE)['oldest_age_s']
    assert isinstance(age, int)
    assert 3500 <= age <= 3700


def test_the_summary_of_an_empty_outbox_has_no_age(records):
    """An empty queue has no records and no age."""
    summary = records.outbox_summary(DEFAULT_NAMESPACE)
    assert summary['records'] == 0
    assert summary['oldest_age_s'] is None


def test_a_record_without_a_usable_timestamp_has_no_age(records):
    """A record the age rule cannot read leaves the age unknown."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'message': 'no ts'})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': 'not-a-timestamp'})
    summary = records.outbox_summary(DEFAULT_NAMESPACE)
    assert summary['records'] == 2
    assert summary['oldest_age_s'] is None


def test_a_naive_timestamp_is_read_as_utc(records):
    """The age rule reads a timestamp without a zone as UTC."""
    naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': naive.isoformat()})
    age = records.outbox_summary(DEFAULT_NAMESPACE)['oldest_age_s']
    assert isinstance(age, int)
    assert 3500 <= age <= 3700


def test_a_timestamp_in_the_future_is_clamped_to_zero(records):
    """A clock that runs ahead does not make a record look very fresh."""
    ahead = datetime.now(UTC) + timedelta(hours=1)
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': ahead.isoformat()})
    assert records.outbox_summary(DEFAULT_NAMESPACE)['oldest_age_s'] == 0


def test_an_aged_record_next_to_an_unknown_one_reports_the_known_age(records):
    """An unknown timestamp does not hide a known one."""
    old = datetime.now(UTC) - timedelta(hours=1)
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': old.isoformat()})
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'message': 'no ts'})
    age = records.outbox_summary(DEFAULT_NAMESPACE)['oldest_age_s']
    assert isinstance(age, int)
    assert 3500 <= age <= 3700


# -- atomicity ------------------------------------------------------------


def test_the_record_view_declares_the_atomic_tier(records):
    """The sqlite record view is an atomic store (ADR 0012 decision 9)."""
    assert records.atomic_transactions is True


def test_a_queued_record_is_only_visible_after_the_transaction_commits(
    db_path, records
):
    """The append joins the open transaction instead of committing on its own."""
    other = SqliteStore(db_path)
    try:
        with records.transaction(DEFAULT_NAMESPACE, PLAN) as acquired:
            assert acquired is True
            records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
            assert other.records().read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []
        entries = other.records().read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert len(entries) == 1
    finally:
        other.close()


def test_a_raising_transaction_rolls_back_the_manifest_and_the_outbox(
    records,
):
    """A raising block leaves neither the manifest nor the queued record."""
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, {'version': 1})
    with pytest.raises(RuntimeError), records.transaction(
        DEFAULT_NAMESPACE, PLAN
    ) as acquired:
        assert acquired is True
        records.put_manifest(DEFAULT_NAMESPACE, PLAN, {'version': 2})
        records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        raise RuntimeError('boom')
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) == {'version': 1}
    assert records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []


def test_a_raising_transaction_rolls_an_ack_back(records):
    """An ack made inside a raising block leaves the queue as it was."""
    records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    with pytest.raises(RuntimeError), records.transaction(
        DEFAULT_NAMESPACE, PLAN
    ) as acquired:
        assert acquired is True
        records.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']])
        raise RuntimeError('boom')
    assert len(records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1


def test_a_completed_transaction_commits_the_manifest_and_the_outbox(records):
    with records.transaction(DEFAULT_NAMESPACE, PLAN) as acquired:
        assert acquired is True
        records.put_manifest(DEFAULT_NAMESPACE, PLAN, {'version': 2})
        records.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) == {'version': 2}
    entries = records.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 1
    assert entries[0]['record'] == {'n': 0}


# -- the documentation ----------------------------------------------------


def _reference_blocks() -> list[str]:
    """The REFERENCE.md blocks, one per paragraph and one per table row."""
    text = (Path(__file__).resolve().parents[2] / 'REFERENCE.md').read_text(
        encoding='utf-8'
    )
    blocks: list[str] = []
    for chunk in text.split('\n\n'):
        lines = [line for line in chunk.split('\n') if line.strip()]
        if not lines:
            continue
        if all(line.lstrip().startswith('|') for line in lines):
            blocks.extend(lines)
        else:
            blocks.append('\n'.join(lines))
    return blocks


def test_reference_documents_the_outbox_beside_the_sqlite_store():
    """``PIPELINE_STORE=sqlite`` is documented together with the outbox."""
    blocks = [
        block
        for block in _reference_blocks()
        if 'PIPELINE_STORE' in block and 'sqlite' in block.lower()
    ]
    assert blocks, 'REFERENCE.md must document PIPELINE_STORE=sqlite'
    assert _outbox_store_blocks(), (
        'the PIPELINE_STORE=sqlite documentation must mention the outbox'
    )


def _outbox_store_blocks() -> list[str]:
    """The REFERENCE.md blocks that talk about the outbox of the database store."""
    return [
        block
        for block in _reference_blocks()
        if 'outbox' in block.lower()
        and any(
            token in block.lower()
            for token in ('sqlite', 'database', 'pipeline_store')
        )
    ]


def test_reference_says_the_outbox_spools_are_not_imported_by_sqlite():
    """The docs state that the file-store spools are not imported."""
    blocks = _outbox_store_blocks()
    assert blocks, 'REFERENCE.md must document the sqlite outbox'
    says_not_imported = any(
        'import' in block.lower() or 'migrat' in block.lower()
        for block in blocks
    ) and any(
        re.search(
            r'\b(not|never|no|nothing|without|neither)\b', block, re.IGNORECASE
        )
        for block in blocks
    )
    assert says_not_imported, (
        'the sqlite outbox documentation must say the spool files are not imported'
    )


def test_reference_names_the_drain_step_for_switching_to_sqlite():
    """The docs name the operator step: drain the queues before switching."""
    blocks = _outbox_store_blocks()
    assert blocks, 'REFERENCE.md must document the sqlite outbox'
    names_the_step = any(
        'drain' in block.lower() for block in blocks
    ) or any(
        'drain' in block.lower()
        and 'outbox' in block.lower()
        and any(
            token in block.lower() for token in ('sqlite', 'switch', 'before')
        )
        for block in _reference_blocks()
    )
    assert names_the_step, (
        'the sqlite outbox documentation must name the drain step'
    )


# -- crash safety ---------------------------------------------------------

_CHILD = """
import os
import sys

sys.path.insert(0, {repo!r})

from pipeline.store_sqlite import SqliteStore

db_path = sys.argv[1]
mode = sys.argv[2]

store = SqliteStore(db_path)
records = store.records()
with records.transaction('default', 'plan-a') as acquired:
    assert acquired is True
    records.put_manifest('default', 'plan-a', {{'version': 2}})
    if mode == 'after-manifest':
        os._exit(1)
    records.append_outbox('default', 'plan-a', 'email-a', {{'n': 0}})
    if mode == 'after-outbox':
        os._exit(1)
os._exit(0)
"""


def _run_child(repo_root: str, db_path: str, mode: str, plan_dir: Path) -> int:
    """Run the crash child against ``db_path`` and return its exit code."""
    env = dict(os.environ)
    env['PLAN_DIR'] = str(plan_dir)
    env['PYTHONPATH'] = repo_root
    script = _CHILD.format(repo=repo_root)
    result = subprocess.run(
        [sys.executable, '-c', script, db_path, mode],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.returncode


@contextlib.contextmanager
def _reopen(db_path: str):
    """Open the database again, as a fresh process would."""
    store = SqliteStore(db_path)
    try:
        yield store.records()
    finally:
        store.close()


def _write_manifest(db_path: str, plan: str, version: int) -> None:
    """Store a manifest outside any transaction, so it is already durable."""
    store = SqliteStore(db_path)
    try:
        store.records().put_manifest(DEFAULT_NAMESPACE, plan, {'version': version})
    finally:
        store.close()


def test_a_crash_before_the_outbox_append_leaves_the_old_manifest(db_path, plan_dir):
    """A process that dies mid-transaction commits neither write."""
    _write_manifest(db_path, PLAN, 1)
    repo_root = str(Path(__file__).resolve().parents[2])
    assert _run_child(repo_root, db_path, 'after-manifest', plan_dir) == 1
    with _reopen(db_path) as view:
        assert view.get_manifest(DEFAULT_NAMESPACE, PLAN) == {'version': 1}
        assert view.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []


def test_a_crash_after_the_outbox_append_rolls_both_writes_back(db_path, plan_dir):
    """A crash before the block exits leaves the queue empty as well."""
    _write_manifest(db_path, PLAN, 1)
    repo_root = str(Path(__file__).resolve().parents[2])
    assert _run_child(repo_root, db_path, 'after-outbox', plan_dir) == 1
    with _reopen(db_path) as view:
        assert view.get_manifest(DEFAULT_NAMESPACE, PLAN) == {'version': 1}
        assert view.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []


def test_a_child_that_finishes_its_transaction_leaves_both_writes(db_path, plan_dir):
    """A transaction that committed before the crash is durable."""
    repo_root = str(Path(__file__).resolve().parents[2])
    assert _run_child(repo_root, db_path, 'complete', plan_dir) == 0
    with _reopen(db_path) as view:
        assert view.get_manifest(DEFAULT_NAMESPACE, PLAN) == {'version': 2}
        entries = view.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert len(entries) == 1
        assert entries[0]['record'] == {'n': 0}
