"""The sqlite store's outbox table (ADR 0012 decisions 3 and 9).

The per-channel notification outbox used to be a set of JSONL spool files under
the plan directory, written through the internal file store.  ADR 0012 decision
9 keeps the queue records in the record store next to the state they are derived
from, so a store can commit an outbox append together with the manifest row
that produced it.  For a ``PIPELINE_STORE=sqlite`` deployment that means a row
in the ``outbox`` table below, written on the store's own connection -- the
same ``BEGIN IMMEDIATE`` transaction, when the caller is holding one.

Layout
------
``id``         ``INTEGER PRIMARY KEY AUTOINCREMENT``: the queue's own order,
               handed back to callers as a string.
``namespace``  the record namespace the queue belongs to.
``plan``       the plan the queue belongs to.
``channel``    the notification channel instance (or ``_legacy``).
``ts``         the record's top-level ``ts`` when it is a string, else ``NULL``.
               It is extracted at append time so a summary can read ages from
               this column and never has to touch the payload.
``record``     the queued record as JSON text, sorted keys.

Every statement here is parameterized: a record is data, never SQL.  The
module is pure plumbing over an already-open connection -- it opens none of its
own, and it never resolves a path, so it cannot write a spool file even by
accident.

No migration is performed: rows that a file-store deployment left in
``<plan>.outbox*.jsonl`` are not imported when a deployment switches to the
sqlite store.  An operator drains the old queues before switching.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pipeline.outbox_health import _age_seconds

#: The queue table, in the shape the record store contract needs.
CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS outbox ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "namespace TEXT NOT NULL, "
    "plan TEXT NOT NULL, "
    "channel TEXT NOT NULL, "
    "ts TEXT, "
    "record TEXT NOT NULL"
    ")"
)

#: Reads and acks always filter on the queue, so the index leads with it; the
#: trailing ``id`` makes the oldest-first read an index scan.
CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS outbox_queue_id "
    "ON outbox (namespace, plan, channel, id)"
)

#: Statements executed once when a store opens its database.
SCHEMA_STATEMENTS = (CREATE_TABLE, CREATE_INDEX)


def ensure_schema(conn) -> None:
    """Create the outbox table and its index on ``conn`` when they are absent."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def _ts_of(record: dict[str, Any]) -> str | None:
    """Return the record's top-level ``ts`` when it is a string, else ``None``.

    Only a string can be an outbox timestamp; anything else is unknown, and the
    column is ``NULL`` so a summary reports no age for it rather than inventing
    one from whatever the payload happened to hold.
    """
    value = record.get("ts")
    return value if isinstance(value, str) else None


def append(conn, namespace: str, plan: str, channel: str, record: dict) -> str:
    """Queue ``record`` on ``(namespace, plan, channel)`` and return its id.

    The record is serialised before the statement runs, so a record that cannot
    be stored leaves no row behind.
    """
    payload = json.dumps(record, sort_keys=True)
    cursor = conn.execute(
        "INSERT INTO outbox (namespace, plan, channel, ts, record) "
        "VALUES (?, ?, ?, ?, ?)",
        (namespace, plan, channel, _ts_of(record), payload),
    )
    return str(cursor.lastrowid)


def read(
    conn,
    namespace: str,
    plan: str,
    channel: str,
    limit: int | None = None,
) -> list[dict]:
    """Return the queue's entries oldest first, at most ``limit`` of them."""
    if limit is not None and limit <= 0:
        return []
    sql = (
        "SELECT id, record FROM outbox "
        "WHERE namespace = ? AND plan = ? AND channel = ? ORDER BY id"
    )
    params: list[Any] = [namespace, plan, channel]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [{"id": str(row[0]), "record": json.loads(row[1])} for row in rows]


def ack(conn, namespace: str, plan: str, channel: str, ids) -> int:
    """Remove one pending entry per id in ``ids`` and return how many went."""
    removed = 0
    for entry_id in ids:
        key = str(entry_id)
        # ``rowid`` is the integer the id was rendered from; comparing it as
        # text would let a non-numeric id match a row it does not own.
        if not key.isdigit():
            continue
        cursor = conn.execute(
            "DELETE FROM outbox "
            "WHERE id = ? AND namespace = ? AND plan = ? AND channel = ?",
            (int(key), namespace, plan, channel),
        )
        removed += cursor.rowcount
    return removed


def list_queues(conn, namespace: str) -> list[tuple[str, str]]:
    """Return the sorted ``(plan, channel)`` pairs that hold at least one row."""
    rows = conn.execute(
        "SELECT DISTINCT plan, channel FROM outbox WHERE namespace = ? "
        "ORDER BY plan, channel",
        (namespace,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def summary(conn, namespace: str, plan: str | None = None) -> dict:
    """Summarise the queues with a timestamp-only read of the table.

    The payload column is never selected: the count and the oldest age both
    come out of a queue grouped read and the ``ts`` column, so a summary cannot
    leak a queued record.
    """
    sql = "SELECT plan, channel, ts FROM outbox WHERE namespace = ?"
    params: list[Any] = [namespace]
    if plan is not None:
        sql += " AND plan = ?"
        params.append(plan)
    rows = conn.execute(sql, tuple(params)).fetchall()
    now = datetime.now(UTC)
    queues: set[tuple[str, str]] = set()
    records = 0
    oldest: int | None = None
    for row_plan, row_channel, ts in rows:
        queues.add((row_plan, row_channel))
        records += 1
        age = _age_seconds(ts, now)
        if age is not None and (oldest is None or age > oldest):
            oldest = age
    return {
        "files": len(queues),
        "records": records,
        "oldest_age_s": oldest,
    }
