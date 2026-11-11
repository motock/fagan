"""The versioned schema of the ``SqliteStore`` database.

The database the store keeps grew a table at a time, each created with
``CREATE TABLE IF NOT EXISTS`` when a store opened: the plan manifests, the
outbox queues (ADR 0012 decision 9), the notification history, the usage
accounting and the audit chain with its append-only guards (ADR 0012 decision
10).  ``IF NOT EXISTS`` cannot tell a database that already has a table from one
that needs a change, and it cannot recognise a database written by a newer
build.

So the schema has an explicit version: the ``schema_version`` table holds a
single row naming the version the database is at, and :data:`MIGRATIONS` is the
ordered list of steps that take it from one version to the next.  A database
that predates versioning has no version row, which reads as version 0; its
tables are already migration 1's, so the first open adopts it at version 1
without rewriting a row.

Every pending migration runs in one ``BEGIN IMMEDIATE`` transaction, so a
database is at exactly one version: a migration that raises rolls back and
leaves the previous version and its rows in place.  A database whose version is
newer than this code knows is refused rather than opened, and the refusal names
the two versions and never the path, which may identify an operator's home
directory.

The schema code lives here rather than in ``pipeline/store_sqlite.py`` so the
store stays the store's behaviour; migration 1 reuses each table's definition
from the module that owns it, so a table has one definition and not two.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time

from pipeline import (
    store_sqlite_audit,
    store_sqlite_notifications,
    store_sqlite_outbox,
    store_sqlite_usage,
)

#: The table holding the version of the schema, one row.
SCHEMA_VERSION_TABLE = 'schema_version'

#: One row, and the ``CHECK`` keeps it one row: a version is a fact about the
#: database, not a history of how it got there.
_VERSION_TABLE = (
    'CREATE TABLE IF NOT EXISTS schema_version ('
    'id INTEGER PRIMARY KEY CHECK (id = 1), '
    'version INTEGER NOT NULL)'
)
_SELECT_VERSION = 'SELECT version FROM schema_version WHERE id = 1'
_CLEAR_VERSION = 'DELETE FROM schema_version'
_INSERT_VERSION = 'INSERT INTO schema_version (id, version) VALUES (1, ?)'

#: The plan manifests: one row per plan, the manifest as JSON.
MANIFEST_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS story_manifests ('
    'plan_name TEXT PRIMARY KEY, '
    'manifest_json TEXT NOT NULL'
    ')'
)

_MISSING_TABLE = 'no such table'

#: How long one open waits for the write lock before giving up.  Opening a
#: store is not a write path, so a lock held by another process is waited out
#: only briefly: a database that cannot be migrated now is migrated next time.
_BUSY_DEADLINE_S = 1.0
_BUSY_RETRY_S = 0.01


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """True when a statement lost the race for the database's write lock."""
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is None:
        message = str(exc).lower()
        return 'locked' in message or 'busy' in message
    return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _migration_1(conn: sqlite3.Connection) -> None:
    """The schema as the prerequisite plans left it, tables and guards.

    Every statement is ``IF NOT EXISTS``, which is what lets a database written
    before versioning be adopted as version 1: its tables are already this
    migration's, so nothing is recreated and no row is rewritten.
    """
    conn.execute(MANIFEST_SCHEMA)
    store_sqlite_outbox.ensure_schema(conn)
    store_sqlite_notifications.ensure_schema(conn)
    conn.execute(store_sqlite_usage.USAGE_SCHEMA)
    store_sqlite_audit.ensure_schema(conn)


#: The record tables: the decisions and the checkpoint journals (migration 2),
#: then the plan drafts, the completion records and the context snapshots
#: (migration 3).  Every statement is parameterized at its use site; these are
#: only the shapes.
_DECISIONS_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS decisions (namespace TEXT NOT NULL, '
    'plan TEXT NOT NULL, seq INTEGER NOT NULL, record_json TEXT NOT NULL, '
    'PRIMARY KEY (namespace, plan, seq))'
)
_JOURNAL_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS journal (namespace TEXT NOT NULL, '
    'plan TEXT NOT NULL, story_key TEXT NOT NULL, seq INTEGER NOT NULL, '
    'record_json TEXT NOT NULL, created_at REAL NOT NULL, '
    'PRIMARY KEY (namespace, plan, story_key, seq))'
)
_PLAN_DRAFTS_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS plan_drafts (namespace TEXT NOT NULL, '
    'plan TEXT NOT NULL, plan_json TEXT NOT NULL, '
    'PRIMARY KEY (namespace, plan))'
)
_PLAN_COMPLETIONS_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS plan_completions (namespace TEXT NOT NULL, '
    'plan TEXT NOT NULL, completed_at TEXT, report_text TEXT, '
    'PRIMARY KEY (namespace, plan))'
)
_CONTEXT_SNAPSHOTS_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS context_snapshots (namespace TEXT NOT NULL, '
    'digest TEXT NOT NULL, content TEXT NOT NULL, meta_json TEXT NOT NULL, '
    'PRIMARY KEY (namespace, digest))'
)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Store the decisions and the checkpoint journals as table rows."""
    conn.execute(_DECISIONS_SCHEMA)
    conn.execute(_JOURNAL_SCHEMA)


def _migration_3(conn: sqlite3.Connection) -> None:
    """Store the plan drafts, the completion records and the snapshots."""
    conn.execute(_PLAN_DRAFTS_SCHEMA)
    conn.execute(_PLAN_COMPLETIONS_SCHEMA)
    conn.execute(_CONTEXT_SNAPSHOTS_SCHEMA)


#: The migrations, in order.  The version a database is at is how many of them
#: it has run, so a migration is appended and never renumbered.
MIGRATIONS = (_migration_1, _migration_2, _migration_3)


def latest_version() -> int:
    """Return the version this code migrates a database to."""
    return len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    """Return the version *conn*'s database is at, ``0`` when it has none.

    A database without a ``schema_version`` table predates versioning, and its
    tables are migration 1's, so it is at version 0 and one migration away.
    """
    try:
        row = conn.execute(_SELECT_VERSION).fetchone()
    except sqlite3.OperationalError as exc:
        if _MISSING_TABLE not in str(exc).lower():
            raise
        return 0
    return int(row[0]) if row else 0


def _newer_version(version: int, latest: int) -> ValueError:
    """The refusal of a database written by a newer build."""
    return ValueError(
        f'the database schema is at version {version}, '
        f'newer than the version {latest} this code knows'
    )


def _begin(conn: sqlite3.Connection) -> None:
    """Open a write transaction, waiting out a lock held by another process."""
    deadline = time.monotonic() + _BUSY_DEADLINE_S
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_BUSY_RETRY_S)


@contextlib.contextmanager
def _write_transaction(conn: sqlite3.Connection):
    """Hold the database's write lock for the block, commit or roll back."""
    _begin(conn)
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    """Write *version* as the database's version."""
    conn.execute(_CLEAR_VERSION)
    conn.execute(_INSERT_VERSION, (version,))


def migrate(conn: sqlite3.Connection) -> int:
    """Bring *conn*'s database up to :func:`latest_version`, return its version.

    A database already at the latest version is left alone without taking the
    write lock, which is what makes opening one twice a read.  A database
    behind runs its pending migrations inside one transaction, so a migration
    that raises leaves the previous version and its rows in place.  A database
    from a newer build is refused with :class:`ValueError` before anything is
    written to it.
    """
    latest = latest_version()
    version = current_version(conn)
    if version > latest:
        raise _newer_version(version, latest)
    if version == latest:
        return version
    with _write_transaction(conn):
        # Another process may have migrated the database while this one waited
        # for the write lock, so the version is read again inside it.
        conn.execute(_VERSION_TABLE)
        version = current_version(conn)
        if version > latest:
            raise _newer_version(version, latest)
        if version == latest:
            return version
        for migration in MIGRATIONS[version:]:
            migration(conn)
        _record_version(conn, latest)
    return latest
