"""The sqlite store's notification history table (ADR 0012 decision 9).

A plan's notification history used to be two files beside its manifest: the
free-text ``<plan>.notifications.log`` line and the structured
``<plan>.notifications.jsonl`` record.  A ``PIPELINE_STORE=sqlite`` deployment
keeps that history in the record store instead, next to the state it is derived
from, so a notification written inside a transaction commits (or rolls back)
with the rest of the transaction.  Every notification is one row in the
``notifications`` table below, written on the store's own connection -- the
same ``BEGIN IMMEDIATE`` transaction, when the caller is holding one.

Layout
------
``id``         ``INTEGER PRIMARY KEY AUTOINCREMENT``: the history's own order,
               so append order survives a clock that steps backwards.
``namespace``  the record namespace the history belongs to.
``plan``       the plan the history belongs to.
``ts``         the record's top-level ``ts`` as text, so the free-text line is
               ``f'{ts} {message}'`` without parsing the payload.
``record``     the whole notification record as JSON text.

Every statement here is parameterized: a record is data, never SQL.  The module
is pure plumbing over an already-open connection -- it opens none of its own,
and it never resolves a path, so it cannot write a sidecar file even by
accident.

Two open questions are recorded rather than answered here.  There is no
retention cap: the file store rotated its sidecars by size, which is a file
concept, and a row store needs a row-count or age policy of its own.  And,
exactly like the sibling ``outbox`` table, no migration is performed: rows that
a file-store deployment left in ``<plan>.notifications.log`` /
``<plan>.notifications.jsonl`` are not imported when a deployment switches to
the sqlite store.  An operator archives or replays those sidecars before
switching.
"""

from __future__ import annotations

import json
from typing import Any

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    UnknownNamespaceError,
    validate_namespace,
    validate_notification_record,
    validate_plan_name,
)

__all__ = [
    'DEFAULT_LINE_LIMIT',
    'SCHEMA_STATEMENTS',
    'NotificationHistoryMixin',
    'ensure_schema',
    'read_records',
]

#: How many entries a ``limit`` of zero or less asks for (the legacy rule).
DEFAULT_LINE_LIMIT = 100

#: The severities a notification record may carry; anything else is ``info``.
SEVERITIES = ('info', 'warning', 'error')

#: The table, in the shape the record store contract needs.
CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS notifications ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "namespace TEXT NOT NULL, "
    "plan TEXT NOT NULL, "
    "ts TEXT NOT NULL, "
    "record TEXT NOT NULL"
    ")"
)

#: Every read filters on the history it serves, so the index leads with it; the
#: trailing ``id`` makes the oldest-first read an index scan.
CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS notifications_history_id "
    "ON notifications (namespace, plan, id)"
)

#: Statements executed once when a store opens its database.
SCHEMA_STATEMENTS = (CREATE_TABLE, CREATE_INDEX)

#: The oldest-first read of one plan's history.
SELECT_HISTORY = (
    "SELECT record FROM notifications "
    "WHERE namespace = ? AND plan = ? ORDER BY id ASC"
)

#: The append of one notification, every field bound as a parameter.
INSERT_HISTORY = (
    "INSERT INTO notifications (namespace, plan, ts, record) "
    "VALUES (?, ?, ?, ?)"
)


def ensure_schema(conn) -> None:
    """Create the notifications table and its index on ``conn`` when absent."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def read_records(conn, namespace: str, plan_name: str) -> list[dict[str, Any]]:
    """Return one plan's history oldest first, as the records that were stored.

    A stored row that no longer parses is not a record: it is skipped rather
    than served, so damage outside this store cannot make a dashboard read
    fail.  There is nothing to count, either -- a row store writes no JSONL
    sidecar, so no line was unreadable.
    """
    records: list[dict[str, Any]] = []
    for (payload,) in conn.execute(SELECT_HISTORY, (namespace, plan_name)):
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def notification_line(record: dict[str, Any]) -> str:
    """Return the free-text line a record renders as: ``f'{ts} {message}'``.

    A message-less notification renders as ``'<ts> '``, the shape
    :func:`pipeline.persistence._write_notification` writes for it -- never the
    literal ``'None'``.
    """
    return f"{record.get('ts') or ''} {record.get('message') or ''}"


def _normalized(record: dict[str, Any]) -> dict[str, Any]:
    """Return ``record`` in the shape the legacy reader has always produced.

    This is :meth:`pipeline.store.FileStore.get_notification_records`'s
    normalization, field for field: six keys, a severity outside
    ``info``/``warning``/``error`` reported as ``info``, and every other value
    coerced or defaulted the same way.
    """
    severity = record.get('severity')
    if severity not in SEVERITIES:
        severity = 'info'
    else:
        severity = str(severity)
    return {
        'ts': str(record.get('ts', '')),
        'message': str(record.get('message', '')),
        'severity': severity,
        'story_key': record.get('story_key'),
        'event': record.get('event'),
        'dedup_key': record.get('dedup_key'),
    }


class NotificationHistoryMixin:
    """The notification history of a sqlite-backed store.

    The mixin holds no state of its own beyond one per-instance flag: it works
    on the connection the store already opened (``self._conn``, or the wrapped
    store's ``_conn`` when the class is the record view), so an append inside a
    caller's transaction is the caller's row and rolls back with it.

    Every public method validates its keys *first* -- an unknown namespace or a
    malformed plan name is refused before any statement runs, exactly as the
    file store refuses it before touching a path.
    """

    # -- plumbing ------------------------------------------------------
    def _notification_conn(self):
        """Return the store's shared connection."""
        conn = getattr(self, '_conn', None)
        if conn is not None:
            return conn
        return self._store._conn

    def _validate_history(self, namespace: str, plan_name: str) -> None:
        """Validate the keys of one history, refusing an unserved namespace."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError('unknown namespace')

    def _ensure_notification_schema(self) -> None:
        """Create the history table when this instance has not yet done so.

        ``SqliteStore.__init__`` also creates it when the database is opened,
        like the sibling ``outbox`` schema, so the table an append inside a
        caller's transaction writes is never created inside that transaction
        (a table created there would roll back with it).
        """
        if getattr(self, '_notification_schema_ready', False):
            return
        ensure_schema(self._notification_conn())
        self._notification_schema_ready = True

    def _history_records(self, namespace: str, plan_name: str) -> list[dict]:
        """Return the validated history of one plan, oldest first."""
        self._validate_history(namespace, plan_name)
        self._ensure_notification_schema()
        return read_records(self._notification_conn(), namespace, plan_name)

    def _history_lines(
        self, namespace: str, plan_name: str, limit: int = DEFAULT_LINE_LIMIT
    ) -> list[str]:
        """Return the last ``limit`` free-text lines of one plan's history."""
        lines = [
            notification_line(record)
            for record in self._history_records(namespace, plan_name)
        ]
        if limit <= 0:
            limit = DEFAULT_LINE_LIMIT
        return lines[-limit:]

    # -- the record API ------------------------------------------------
    def append_notification(
        self, namespace: str, plan_name: str, record: dict
    ) -> None:
        """Append one notification to ``plan_name``'s history.

        The record is serialised before the statement runs, so a record that
        cannot be stored leaves no row behind.  The insert goes to the store's
        own connection: inside a caller's transaction it is part of that
        transaction, and outside one the driver commits it when the call
        returns (the connection is in autocommit mode, so no explicit commit is
        issued -- committing here would end a transaction the caller still
        holds, and commit the rows a rollback should discard).
        """
        self._validate_history(namespace, plan_name)
        validate_notification_record(record)
        payload = json.dumps(record)
        ts = record['ts']
        self._ensure_notification_schema()
        self._notification_conn().execute(
            INSERT_HISTORY, (namespace, plan_name, str(ts), payload)
        )

    def get_notification_history(
        self, namespace: str, plan_name: str
    ) -> tuple[list[dict], int]:
        """Return ``(records, 0)``: the history oldest first, and no junk.

        A row store writes no JSONL sidecar, so there is never an unreadable
        line to count and the second element is always ``0``.  A plan with no
        notifications is ``([], 0)`` and is not created.  The read holds no
        cursor state, so calling it twice answers the same thing twice.
        """
        return self._history_records(namespace, plan_name), 0

    def get_notification_lines(
        self, namespace: str, plan_name: str, limit: int = DEFAULT_LINE_LIMIT
    ) -> list[str]:
        """Return the last ``limit`` notification lines, each ``f'{ts} {message}'``.

        The slicing is the legacy reader's own: a ``limit`` of zero or less
        means the last 100, and the newest entries win.
        """
        return self._history_lines(namespace, plan_name, limit)

    # -- the legacy readers --------------------------------------------
    def get_notifications(self, plan_name: str) -> list[str]:
        """Return the last 100 free-text lines of ``plan_name``'s history."""
        return self._history_lines(DEFAULT_NAMESPACE, plan_name, DEFAULT_LINE_LIMIT)

    def get_notification_records(
        self, plan_name: str, limit: int = DEFAULT_LINE_LIMIT
    ) -> list[dict]:
        """Return the last ``limit`` records in the legacy reader's shape.

        The normalization is :meth:`pipeline.store.FileStore.
        get_notification_records`'s, so a dashboard that grew up on the file
        store reads the same values out of the table.
        """
        records = [
            _normalized(record)
            for record in self._history_records(DEFAULT_NAMESPACE, plan_name)
        ]
        if limit <= 0:
            limit = DEFAULT_LINE_LIMIT
        return records[-limit:]
