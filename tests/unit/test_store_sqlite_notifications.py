"""``SqliteStore``'s notification-history table (ADR 0012 decision 9).

A ``PIPELINE_STORE=sqlite`` deployment keeps a plan's notification history in
the database, not in the ``<plan>.notifications.log`` / ``.jsonl`` sidecars the
file store writes: one row per notification in a ``notifications`` table
(``id``, ``namespace``, ``plan``, ``ts``, ``record``), indexed by
``(namespace, plan, id)``.

The behaviour is exercised through the public store API -- the record view's
``append_notification`` / ``get_notification_history`` /
``get_notification_lines`` and the legacy ``SqliteStore.get_notifications`` /
``get_notification_records`` -- and the database file itself is inspected only
to pin the storage the story names: the table, its columns and its index.

A notification history is a *record*, not a file: a sidecar left behind by an
earlier file-store deployment is neither imported nor served, so the operator
must archive it before switching stores.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
OTHER_PLAN = 'plan-b'

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _notification_rows(db_path: str) -> list[sqlite3.Row]:
    """Every row of the ``notifications`` table."""
    return _rows(db_path, 'SELECT * FROM notifications')


def _table_columns(db_path: str, table: str) -> dict[str, sqlite3.Row]:
    """The ``PRAGMA table_info`` rows of ``table``, keyed by column name."""
    return {row['name']: row for row in _rows(db_path, f'PRAGMA table_info({table})')}


def _record(message: str, ts: str = '2026-01-01T00:00:00+00:00', **extra):
    """A notification record with the fields a writer puts in one."""
    record = {
        'ts': ts,
        'message': message,
        'severity': 'info',
        'story_key': 'story-a',
        'event': 'story_started',
    }
    record.update(extra)
    return record


# -- the table ------------------------------------------------------------


def test_the_notifications_table_exists_with_the_documented_columns(db_path, records):
    """The history is a table with the columns the story names."""
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('hello'))

    columns = _table_columns(db_path, 'notifications')
    assert set(columns) == {'id', 'namespace', 'plan', 'ts', 'record'}
    assert columns['id']['type'].upper() == 'INTEGER'
    assert columns['id']['pk'] == 1
    for required in ('namespace', 'plan', 'ts', 'record'):
        assert columns[required]['notnull'] == 1, f'{required} must be NOT NULL'


def test_the_notifications_id_is_an_autoincrementing_integer(db_path, records):
    """``id`` is an ``INTEGER PRIMARY KEY AUTOINCREMENT`` column."""
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('one'))
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('two'))

    schema = _rows(
        db_path,
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'",
    )
    assert schema, 'the notifications table must exist'
    assert 'autoincrement' in schema[0]['sql'].lower()

    ids = [row['id'] for row in _notification_rows(db_path)]
    assert len(ids) == 2
    assert all(isinstance(value, int) for value in ids)
    assert ids[1] > ids[0]


def test_the_notifications_table_is_indexed_by_namespace_plan_and_id(db_path, records):
    """An index covers ``(namespace, plan, id)``."""
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('hello'))

    indexes = _rows(db_path, "PRAGMA index_list('notifications')")
    assert indexes, 'the notifications table must carry an index'
    indexed = []
    for index in indexes:
        indexed.append(
            [
                row['name']
                for row in _rows(db_path, f"PRAGMA index_info('{index['name']}')")
            ]
        )
    assert any(
        columns[:3] == ['namespace', 'plan', 'id'] for columns in indexed
    ), f'no index on (namespace, plan, id): {indexed}'


def test_an_appended_notification_is_a_row_in_the_table(db_path, records):
    """One append is one row, carrying the namespace, plan and record."""
    record = _record('hello')

    records.append_notification(DEFAULT_NAMESPACE, PLAN, record)

    rows = _notification_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row['namespace'] == DEFAULT_NAMESPACE
    assert row['plan'] == PLAN
    assert row['ts'] == record['ts']
    assert json.loads(row['record']) == record


# -- the record view's history -------------------------------------------


def test_the_history_returns_the_records_that_were_written(records):
    """``get_notification_history`` serves the records, oldest first."""
    first = _record('first', ts='2026-01-01T00:00:00+00:00')
    second = _record('second', ts='2026-01-01T00:01:00+00:00')

    records.append_notification(DEFAULT_NAMESPACE, PLAN, first)
    records.append_notification(DEFAULT_NAMESPACE, PLAN, second)

    history, malformed = records.get_notification_history(DEFAULT_NAMESPACE, PLAN)
    assert history == [first, second]
    assert malformed == 0


def test_the_history_of_a_plan_that_was_never_written_is_empty(records):
    """A plan nobody notified answers ``([], 0)``."""
    assert records.get_notification_history(DEFAULT_NAMESPACE, PLAN) == ([], 0)


def test_the_history_of_one_plan_does_not_leak_into_another(records):
    """The rows belong to the plan they were appended for."""
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('mine'))

    history, _ = records.get_notification_history(DEFAULT_NAMESPACE, OTHER_PLAN)
    assert history == []


def test_the_lines_render_the_timestamp_and_the_message(records):
    """``get_notification_lines`` renders ``f'{ts} {message}'``."""
    record = _record('a notification', ts='2026-01-01T00:00:00+00:00')
    records.append_notification(DEFAULT_NAMESPACE, PLAN, record)

    lines = records.get_notification_lines(DEFAULT_NAMESPACE, PLAN)
    assert lines == [f"{record['ts']} {record['message']}"]


def test_the_lines_return_the_most_recent_rows_within_the_limit(records):
    """A ``limit`` keeps the last rows, oldest first."""
    for index in range(3):
        records.append_notification(
            DEFAULT_NAMESPACE,
            PLAN,
            _record(f'm{index}', ts=f'2026-01-01T00:0{index}:00+00:00'),
        )

    lines = records.get_notification_lines(DEFAULT_NAMESPACE, PLAN, limit=2)

    assert len(lines) == 2
    assert lines[0].endswith('m1')
    assert lines[1].endswith('m2')


def test_a_limit_of_zero_or_less_means_the_last_hundred(records):
    """The legacy rule: a non-positive limit asks for the last hundred."""
    for index in range(3):
        records.append_notification(
            DEFAULT_NAMESPACE,
            PLAN,
            _record(f'm{index}', ts=f'2026-01-01T00:0{index}:00+00:00'),
        )

    assert len(records.get_notification_lines(DEFAULT_NAMESPACE, PLAN, limit=0)) == 3
    assert len(records.get_notification_lines(DEFAULT_NAMESPACE, PLAN, limit=-1)) == 3


def test_a_notification_is_not_written_to_a_sidecar_file(records, plan_dir):
    """The history never reaches the filesystem."""
    records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('hello'))

    assert list(plan_dir.glob('*.notifications*')) == []


def test_a_sidecar_file_on_disk_is_not_imported(records, plan_dir):
    """An existing file-store sidecar is not served and not counted."""
    (plan_dir / f'{PLAN}.notifications.log').write_text(
        '2024-05-05T10:00:00+00:00 old\n', encoding='utf-8'
    )
    (plan_dir / f'{PLAN}.notifications.jsonl').write_text(
        json.dumps({'ts': '2024-05-05T10:00:00+00:00', 'message': 'old'}) + '\n',
        encoding='utf-8',
    )

    assert records.get_notification_history(DEFAULT_NAMESPACE, PLAN) == ([], 0)
    assert records.get_notification_lines(DEFAULT_NAMESPACE, PLAN) == []


# -- the legacy readers ---------------------------------------------------


def test_the_legacy_readers_are_served_from_the_table(db_path, plan_dir):
    """``get_notifications`` / ``get_notification_records`` read the rows."""
    store = SqliteStore(db_path)
    try:
        store.records().append_notification(
            DEFAULT_NAMESPACE,
            PLAN,
            _record(
                'a notification',
                severity='warning',
                story_key='story-a',
                event='story_parked',
                dedup_key='story-a:story_parked',
            ),
        )

        lines = store.get_notifications(PLAN)
        records_read = store.get_notification_records(PLAN)
    finally:
        store.close()

    assert lines == ['2026-01-01T00:00:00+00:00 a notification']
    assert len(records_read) == 1
    assert records_read[0]['message'] == 'a notification'
    assert records_read[0]['severity'] == 'warning'
    assert records_read[0]['story_key'] == 'story-a'
    assert records_read[0]['event'] == 'story_parked'
    assert records_read[0]['dedup_key'] == 'story-a:story_parked'


def test_the_legacy_readers_see_nothing_when_the_table_is_empty(db_path, plan_dir):
    """A plan nobody notified reads empty through the legacy API too."""
    store = SqliteStore(db_path)
    try:
        assert store.get_notifications(PLAN) == []
        assert store.get_notification_records(PLAN) == []
    finally:
        store.close()


def test_the_legacy_readers_normalize_exactly_like_the_file_store(
    db_path, plan_dir, monkeypatch
):
    """The sqlite readers apply the file store's exact normalization."""
    fixture = [
        _record(
            'first',
            ts='2026-01-01T00:00:00+00:00',
            severity='warning',
            story_key='story-a',
            event='story_started',
            dedup_key='story-a:story_started',
        ),
        _record(
            'second',
            ts='2026-01-01T00:01:00+00:00',
            severity='critical',
            story_key='story-b',
            event='story_parked',
            dedup_key='story-b:story_parked',
        ),
        _record(
            'third',
            ts='2026-01-01T00:02:00+00:00',
            severity='error',
            story_key='story-c',
            event='story_failed',
            dedup_key='story-c:story_failed',
        ),
    ]

    # The same records, read through the file store's own reader.
    file_dir = plan_dir / 'file-store'
    file_dir.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', file_dir)
    (file_dir / f'{PLAN}.notifications.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in fixture),
        encoding='utf-8',
    )
    expected = {
        limit: FileStore().get_notification_records(PLAN, limit)
        for limit in (0, 1, 101)
    }
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', plan_dir)

    store = SqliteStore(db_path)
    try:
        for record in fixture:
            store.records().append_notification(DEFAULT_NAMESPACE, PLAN, record)

        for limit in (0, 1, 101):
            got = store.get_notification_records(PLAN, limit)
            assert got == expected[limit], f'limit={limit}'
    finally:
        store.close()


def test_the_legacy_records_keep_the_file_store_six_keys(db_path, plan_dir):
    """A record is normalized to exactly the six keys the file store serves."""
    store = SqliteStore(db_path)
    try:
        store.records().append_notification(
            DEFAULT_NAMESPACE, PLAN, _record('hello', severity='nonsense')
        )

        got = store.get_notification_records(PLAN)
    finally:
        store.close()

    assert set(got[0]) == {
        'ts',
        'message',
        'severity',
        'story_key',
        'event',
        'dedup_key',
    }
    assert got[0]['severity'] == 'info', 'an unknown severity becomes info'


def test_the_legacy_records_of_another_plan_are_not_served(db_path, plan_dir):
    """The legacy readers are scoped to their plan."""
    store = SqliteStore(db_path)
    try:
        store.records().append_notification(
            DEFAULT_NAMESPACE, PLAN, _record('mine')
        )

        assert store.get_notification_records(OTHER_PLAN) == []
        assert store.get_notifications(OTHER_PLAN) == []
    finally:
        store.close()


# -- refusals write nothing ----------------------------------------------


def test_an_invalid_plan_name_is_refused_before_any_row_is_written(db_path, records):
    """A bad plan name raises and leaves the table empty."""
    with pytest.raises(ValueError):
        records.append_notification(DEFAULT_NAMESPACE, '../escape', _record('x'))

    assert _notification_rows(db_path) == []


def test_an_invalid_namespace_is_refused_before_any_row_is_written(db_path, records):
    """A malformed namespace raises and leaves the table empty."""
    with pytest.raises(ValueError):
        records.append_notification('../escape', PLAN, _record('x'))

    assert _notification_rows(db_path) == []


def test_an_unknown_namespace_is_refused_before_any_row_is_written(db_path, records):
    """Another namespace raises and leaves the table empty."""
    with pytest.raises(UnknownNamespaceError):
        records.append_notification('acme', PLAN, _record('x'))

    assert _notification_rows(db_path) == []


def test_a_record_that_is_not_a_dict_is_refused_before_any_row_is_written(
    db_path, records
):
    """A non-dict record raises and leaves the table empty."""
    with pytest.raises(TypeError):
        records.append_notification(DEFAULT_NAMESPACE, PLAN, 'not a dict')

    assert _notification_rows(db_path) == []


def test_a_record_carrying_sql_is_stored_as_data(db_path, records):
    """A quote-laden message is stored as text, and survives the round trip."""
    payload = "'); DROP TABLE notifications; -- \"quoted\""
    record = _record(payload)

    records.append_notification(DEFAULT_NAMESPACE, PLAN, record)

    rows = _notification_rows(db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]['record']) == record
    history, _ = records.get_notification_history(DEFAULT_NAMESPACE, PLAN)
    assert history == [record]


# -- atomicity ------------------------------------------------------------


def test_a_notification_appended_in_a_transaction_is_rolled_back(db_path, records):
    """A raising transaction leaves no notification row behind."""
    with pytest.raises(RuntimeError), records.transaction(
        DEFAULT_NAMESPACE, PLAN
    ) as acquired:
        assert acquired is True
        records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('dropped'))
        raise RuntimeError('boom')

    assert _notification_rows(db_path) == []
    assert records.get_notification_history(DEFAULT_NAMESPACE, PLAN) == ([], 0)


def test_a_notification_appended_in_a_committed_transaction_is_served(db_path, records):
    """A committed transaction keeps the notification."""
    with records.transaction(DEFAULT_NAMESPACE, PLAN) as acquired:
        assert acquired is True
        records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('kept'))

    history, _ = records.get_notification_history(DEFAULT_NAMESPACE, PLAN)
    assert [record['message'] for record in history] == ['kept']


def test_a_notification_is_only_visible_after_the_transaction_commits(
    db_path, records
):
    """The append joins the open transaction instead of committing on its own."""
    other = SqliteStore(db_path)
    try:
        with records.transaction(DEFAULT_NAMESPACE, PLAN) as acquired:
            assert acquired is True
            records.append_notification(DEFAULT_NAMESPACE, PLAN, _record('pending'))
            assert (
                other.records().get_notification_history(DEFAULT_NAMESPACE, PLAN)
                == ([], 0)
            )
        history, _ = other.records().get_notification_history(DEFAULT_NAMESPACE, PLAN)
        assert len(history) == 1
    finally:
        other.close()


_CHILD = """
import os
import sys

sys.path.insert(0, {repo!r})

from pipeline.store_sqlite import SqliteStore

db_path = sys.argv[1]

store = SqliteStore(db_path)
records = store.records()
with records.transaction('default', 'plan-a') as acquired:
    assert acquired is True
    records.append_notification(
        'default',
        'plan-a',
        {{'ts': '2026-01-01T00:00:00+00:00', 'message': 'crashed'}},
    )
os._exit(1)
"""


def _run_child(db_path: str) -> int:
    """Run the crash child against ``db_path`` and return its exit code."""
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT)
    script = _CHILD.format(repo=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, '-c', script, db_path],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.returncode


def test_a_crash_before_the_commit_leaves_no_notification_row(db_path, plan_dir):
    """A process that dies inside the transaction commits no notification."""
    assert _run_child(db_path) == 1

    store = SqliteStore(db_path)
    try:
        assert store.records().get_notification_history(DEFAULT_NAMESPACE, PLAN) == (
            [],
            0,
        )
    finally:
        store.close()
    assert _notification_rows(db_path) == []


# -- documentation --------------------------------------------------------


def _reference_text() -> str:
    """The whole of ``REFERENCE.md``."""
    return (REPO_ROOT / 'REFERENCE.md').read_text(encoding='utf-8')


def test_the_reference_names_the_file_store_as_the_default_for_notifications():
    """The notification-records section says it describes the file store."""
    text = _reference_text()
    start = text.index('## Notification records')
    body = text[start:].split('\n## ', 1)[0]
    first_paragraph = body.split('\n\n', 1)[1]
    first_sentence = re.split(r'\.\s', first_paragraph.strip(), maxsplit=1)[0]

    assert 'file store' in first_sentence.lower(), first_sentence
    assert 'default' in first_sentence.lower(), first_sentence


def test_the_reference_documents_the_sqlite_notification_table():
    """The sqlite store's notification history is documented as a table."""
    blocks = [
        chunk for chunk in _reference_text().split('\n\n') if chunk.strip()
    ]
    documented = any(
        'notification' in block.lower()
        and 'table' in block.lower()
        and ('sqlite' in block.lower() or 'database' in block.lower())
        for block in blocks
    )
    assert documented, (
        'REFERENCE.md must document the sqlite store notification history'
    )


def test_the_reference_says_the_legacy_sidecars_are_not_imported():
    """The docs state that the file-store notification sidecars are not imported."""
    text = _reference_text()
    assert re.search(
        r'notification[^.]{0,400}?(not|never|no|nothing)\b[^.]{0,200}?'
        r'(import|migrat)',
        text,
        re.IGNORECASE | re.DOTALL,
    ), 'REFERENCE.md must say the notification sidecars are not imported'


def test_the_local_success_report_documents_that_sqlite_has_no_history():
    """The offline report says it cannot see a sqlite deployment's history."""
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, 'scripts/local_success_report.py', '--help'],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    help_text = (result.stdout + result.stderr).lower()
    reference = _reference_text().lower()

    documented_in_help = 'sqlite' in help_text
    documented_in_reference = 'sqlite' in reference and (
        'local_success_report' in reference or 'local success report' in reference
    )

    assert documented_in_help or documented_in_reference, (
        'the offline report must document that it sees no sqlite history'
    )
