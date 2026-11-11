"""``SqliteStore``'s schema is versioned by an ordered list of migrations.

The database the store keeps has grown a table at a time: ``story_manifests``,
then the outbox queues, the notification history, the usage accounting and the
audit chain with its append-only guards.  Each of those was created with
``CREATE TABLE IF NOT EXISTS`` at open, which cannot tell a database that
already has them from one that needs a change, and cannot refuse one written by
a newer build.

This story gives the database an explicit version -- a ``schema_version`` table
plus an ordered list of migrations in ``pipeline/store_sqlite_schema.py``,
applied in one transaction at open.  Migration 1 is exactly the set of tables
the prerequisite plans added, so a database that predates versioning is adopted
as version 1 without any row being rewritten.

Everything is graded through what an operator or another process can observe:
the version row, the rows an existing database still holds, the guards still
refusing an ``UPDATE``, and the refusal of a database from a newer build (which
must name the two versions and never the path).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import store_sqlite_schema
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
MANIFEST = {'plan_name': PLAN, 'stories': {'s1': {'status': 'planned'}}}
AUDIT_MESSAGE = 'audit records are append-only'

#: The database as the prerequisite plans leave it: every table and guard
#: spelled out literally, because a test that imported today's schema would
#: stop describing the database it is supposed to adopt.
_LEGACY_STATEMENTS = (
    (
        'CREATE TABLE IF NOT EXISTS story_manifests ('
        'plan_name TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)'
    ),
    (
        'CREATE TABLE IF NOT EXISTS outbox ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, '
        'plan TEXT NOT NULL, channel TEXT NOT NULL, ts TEXT, record TEXT NOT NULL)'
    ),
    (
        'CREATE INDEX IF NOT EXISTS outbox_queue_id '
        'ON outbox (namespace, plan, channel, id)'
    ),
    (
        'CREATE TABLE IF NOT EXISTS notifications ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, '
        'plan TEXT NOT NULL, ts TEXT NOT NULL, record TEXT NOT NULL)'
    ),
    (
        'CREATE INDEX IF NOT EXISTS notifications_history_id '
        'ON notifications (namespace, plan, id)'
    ),
    (
        'CREATE TABLE IF NOT EXISTS usage ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, '
        'plan TEXT NOT NULL, backend TEXT, model TEXT, '
        'input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)'
    ),
    (
        'CREATE TABLE IF NOT EXISTS audit_records ('
        'namespace TEXT NOT NULL, chain TEXT NOT NULL, seq INTEGER NOT NULL, '
        'prev_hash TEXT NOT NULL, record TEXT NOT NULL, hash TEXT NOT NULL, '
        'PRIMARY KEY (namespace, chain, seq))'
    ),
    (
        'CREATE TRIGGER IF NOT EXISTS audit_records_no_update '
        'BEFORE UPDATE ON audit_records '
        f"BEGIN SELECT RAISE(ABORT, '{AUDIT_MESSAGE}'); END"
    ),
    (
        'CREATE TRIGGER IF NOT EXISTS audit_records_no_delete '
        'BEFORE DELETE ON audit_records '
        f"BEGIN SELECT RAISE(ABORT, '{AUDIT_MESSAGE}'); END"
    ),
)

_LEGACY_ROWS = (
    (
        'INSERT INTO story_manifests (plan_name, manifest_json) VALUES (?, ?)',
        (PLAN, json.dumps(MANIFEST, sort_keys=True)),
    ),
    (
        'INSERT INTO outbox (namespace, plan, channel, record) VALUES (?, ?, ?, ?)',
        ('default', PLAN, 'cli', json.dumps({'plan': PLAN, 'text': 'queued'})),
    ),
    (
        'INSERT INTO notifications (namespace, plan, ts, record) VALUES (?, ?, ?, ?)',
        ('default', PLAN, '2026-01-01T00:00:00Z', json.dumps({'plan': PLAN})),
    ),
    (
        (
            'INSERT INTO usage (namespace, plan, backend, model, input_tokens, '
            'output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)'
        ),
        ('default', PLAN, 'claude', 'sonnet', 10, 20, 0.5),
    ),
    (
        (
            'INSERT INTO audit_records '
            '(namespace, chain, seq, prev_hash, record, hash) '
            'VALUES (?, ?, ?, ?, ?, ?)'
        ),
        ('default', 'host', 1, '0' * 64, json.dumps({'action': 'x'}), 'a' * 64),
    ),
)


@contextlib.contextmanager
def _probe(db_path):
    """An out-of-band connection to the store's database."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _legacy_database(db_path, version=None):
    """Write the pre-versioning database, optionally stamped at *version*."""
    with _probe(db_path) as conn:
        for statement in _LEGACY_STATEMENTS:
            conn.execute(statement)
        for statement, parameters in _LEGACY_ROWS:
            conn.execute(statement, parameters)
    if version is not None:
        _stamp_version(db_path, version)


def _stamp_version(db_path, version):
    """Write the version row a database of an older (or newer) build carries."""
    with _probe(db_path) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_version ('
            'id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)'
        )
        conn.execute('DELETE FROM schema_version')
        conn.execute('INSERT INTO schema_version (id, version) VALUES (1, ?)', (version,))


def _version(db_path):
    """The version rows the database holds, empty when it has no version."""
    with _probe(db_path) as conn:
        try:
            rows = conn.execute('SELECT version FROM schema_version').fetchall()
        except sqlite3.OperationalError:
            return []
    return [row[0] for row in rows]


def _table_names(db_path):
    with _probe(db_path) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _row_counts(db_path):
    """How many rows each table an existing database holds still holds."""
    counts = {}
    with _probe(db_path) as conn:
        for table in (
            'story_manifests',
            'outbox',
            'notifications',
            'usage',
            'audit_records',
        ):
            counts[table] = conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    return counts


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / 'store.db')


@pytest.fixture(autouse=True)
def isolated_plan_dir(tmp_path, monkeypatch):
    """Keep every manifest projection out of the operator's plans directory."""
    plan_dir = tmp_path / 'plans'
    plan_dir.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', plan_dir)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', plan_dir)
    return plan_dir


@contextlib.contextmanager
def _opened(db_path):
    store = SqliteStore(db_path)
    try:
        yield store
    finally:
        store.close()


def _adds_a_column(conn):
    conn.execute('ALTER TABLE notifications ADD COLUMN severity TEXT')


def _fails(conn):
    conn.execute('ALTER TABLE notifications ADD COLUMN severity TEXT')
    raise RuntimeError('the migration could not finish')


def test_a_new_database_gets_the_latest_version(db_path):
    """Opening an empty file creates the schema and records its version."""
    with _opened(db_path):
        pass

    assert store_sqlite_schema.latest_version() == 1
    assert _version(db_path) == [1]


def test_the_version_is_recorded_once(db_path):
    """``schema_version`` holds one row: a version is a fact, not a history."""
    with _opened(db_path):
        pass

    assert len(_version(db_path)) == 1


def test_a_new_database_holds_every_table_of_migration_one(db_path):
    """Migration 1 is the schema all the prerequisite plans added between them."""
    with _opened(db_path):
        pass

    assert _table_names(db_path) >= {
        'schema_version',
        'story_manifests',
        'outbox',
        'notifications',
        'usage',
        'audit_records',
    }


def test_an_unversioned_database_is_adopted_as_version_one(db_path):
    """A database written before versioning is version 1, not an upgrade."""
    _legacy_database(db_path)
    before = _row_counts(db_path)
    assert _version(db_path) == []

    with _opened(db_path):
        pass

    assert _version(db_path) == [1]
    assert _row_counts(db_path) == before


def test_an_adopted_database_still_serves_its_manifest(db_path):
    """Adoption rewrites no row, so the manifest read back is the one written."""
    _legacy_database(db_path)

    with _opened(db_path) as store:
        assert store.get_manifest(PLAN) == MANIFEST


def test_an_adopted_database_keeps_its_append_only_guards(db_path):
    """The audit guards survive adoption: an ``UPDATE`` still aborts."""
    _legacy_database(db_path)

    with _opened(db_path):
        pass

    with _probe(db_path) as conn, pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute("UPDATE audit_records SET record = 'rewritten'")
    assert AUDIT_MESSAGE in str(excinfo.value)


def test_a_version_one_database_with_rows_is_migrated(db_path, monkeypatch):
    """A database one version behind runs the migration and keeps its rows."""
    _legacy_database(db_path, version=1)
    before = _row_counts(db_path)
    monkeypatch.setattr(
        store_sqlite_schema,
        'MIGRATIONS',
        store_sqlite_schema.MIGRATIONS + (_adds_a_column,),
    )
    assert store_sqlite_schema.latest_version() == 2

    with _opened(db_path):
        pass

    assert _version(db_path) == [2]
    assert _row_counts(db_path) == before
    with _probe(db_path) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(notifications)')}
    assert 'severity' in columns, 'the pending migration did not run'


def test_a_new_database_is_migrated_all_the_way(db_path, monkeypatch):
    """Every pending migration runs when a file is opened for the first time."""
    monkeypatch.setattr(
        store_sqlite_schema,
        'MIGRATIONS',
        store_sqlite_schema.MIGRATIONS + (_adds_a_column,),
    )

    with _opened(db_path):
        pass

    assert _version(db_path) == [2]


def test_a_newer_version_is_refused(db_path):
    """A database from a newer build is refused, never opened and written to."""
    _legacy_database(db_path, version=2)
    before = _table_names(db_path)
    rows_before = _row_counts(db_path)

    with pytest.raises(ValueError) as excinfo:
        SqliteStore(db_path)

    message = str(excinfo.value)
    assert '2' in message and '1' in message, (
        'the refusal must name the database version and the code version'
    )
    assert db_path not in message, 'the refusal must never echo the path'
    assert _version(db_path) == [2], 'the version of a refused database must not change'
    assert _table_names(db_path) == before
    assert _row_counts(db_path) == rows_before


def test_a_failing_migration_rolls_back_to_the_prior_version(db_path, monkeypatch):
    """A migration that raises leaves the database at the version it was at."""
    _legacy_database(db_path, version=1)
    monkeypatch.setattr(
        store_sqlite_schema,
        'MIGRATIONS',
        store_sqlite_schema.MIGRATIONS + (_fails,),
    )

    with pytest.raises(RuntimeError):
        SqliteStore(db_path)

    assert _version(db_path) == [1], 'the failed migration must not be recorded'
    with _probe(db_path) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(notifications)')}
    assert 'severity' not in columns, 'the failed migration must be rolled back'
    assert _row_counts(db_path) == {
        'story_manifests': 1,
        'outbox': 1,
        'notifications': 1,
        'usage': 1,
        'audit_records': 1,
    }


def test_a_failing_migration_on_a_new_database_leaves_no_schema(db_path, monkeypatch):
    """A first open that fails records neither a version nor half a schema."""
    monkeypatch.setattr(
        store_sqlite_schema,
        'MIGRATIONS',
        store_sqlite_schema.MIGRATIONS + (_fails,),
    )

    with pytest.raises(RuntimeError):
        SqliteStore(db_path)

    assert _version(db_path) == []
    tables = _table_names(db_path) - {'schema_version'}
    assert tables == set(), f'no migration should be kept: {sorted(tables)}'


def test_opening_twice_is_idempotent(db_path):
    """The second open finds the latest version and writes nothing."""
    with _opened(db_path) as store:
        store.save_manifest(PLAN, MANIFEST)
    snapshot = _row_counts(db_path)

    with _opened(db_path) as store:
        assert store.get_manifest(PLAN) == MANIFEST

    assert _version(db_path) == [1]
    assert _row_counts(db_path) == snapshot


def test_a_refused_database_is_not_left_open(db_path, monkeypatch):
    """A refusal closes the connection it opened before giving up."""
    _legacy_database(db_path, version=2)
    connections = []
    migrate = store_sqlite_schema.migrate

    def record(conn):
        connections.append(conn)
        return migrate(conn)

    monkeypatch.setattr(store_sqlite_schema, 'migrate', record)

    with pytest.raises(ValueError):
        SqliteStore(db_path)

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')
    assert _version(db_path) == [2]
