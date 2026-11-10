"""The ``usage`` table of the sqlite record store (ADR 0012 decision 3).

Usage is billing-grade accounting: a record is appended once and never
rewritten. The table therefore carries one typed column per
:data:`pipeline.store_records.USAGE_RECORD_FIELDS` entry -- the schema enforces
the closed field set as well as the validator does -- and this module offers
exactly an append and a read. No ``UPDATE``, no ``DELETE`` and no upsert exists
anywhere in the codebase, so a stored record cannot be rewritten even from
inside the store.

The table lives here rather than in :mod:`pipeline.store_sqlite` to keep that
module within its line budget. :func:`append_usage_row` writes on the store's
own connection, so an append made inside ``SqliteStore.transaction`` joins the
transaction the store already holds: a rollback, or a process that dies before
the commit, leaves no row behind.

The schema is created by the first append rather than at store construction, so
a database that never recorded usage holds no usage table. A read of a database
with no table serves an empty history rather than failing, which is what a
reader in another process (the dashboard) needs while a writer holds the lock.

Not implemented here, and deliberately: retention per namespace (see
``docs/TELEMETRY_CONTRACT.md`` "Retention and tenancy") is an open question --
nothing prunes this table yet, because a billing-grade row may not be deleted
and the contract has no expiry rule. An existing ``<plan>.usage.jsonl`` sidecar
is not imported either: this story moves the sqlite store's writes, it does not
migrate the file store's history.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from pipeline.store_records import (
    USAGE_RECORD_FIELDS,
    validate_namespace,
    validate_plan_name,
    validate_usage_record,
)

#: Fields stored as a count: an ``INTEGER`` column keeps ``2340`` an int on the
#: way out, which is what the contract's type round trip requires.
_USAGE_INTEGER_FIELDS = (
    'attempt',
    'input_tokens',
    'output_tokens',
    'cache_creation_input_tokens',
    'cache_read_input_tokens',
    'duration_ms',
)

#: Fields stored as a measure: a fractional cost must stay a float.
_USAGE_REAL_FIELDS = ('cost_usd',)

#: Fields a record may never omit, so the column is ``NOT NULL`` too.
_USAGE_REQUIRED_FIELDS = ('ts', 'plan')


def _column_definition(field: str) -> str:
    """Return the ``<name> <type>`` column definition for one usage field."""
    if field in _USAGE_REAL_FIELDS:
        return f'{field} REAL'
    if field in _USAGE_INTEGER_FIELDS:
        return f'{field} INTEGER'
    required = ' NOT NULL' if field in _USAGE_REQUIRED_FIELDS else ''
    return f'{field} TEXT{required}'


#: ``usage`` carries the namespace it belongs to, a surrogate ``id`` that is
#: never reused, and one typed column per contract field. There is no JSON
#: column: the field set is the schema.
USAGE_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS usage ('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'namespace TEXT NOT NULL, '
    + ', '.join(_column_definition(field) for field in USAGE_RECORD_FIELDS)
    + ')'
)

#: The columns a read selects, in contract order, so a served dict has exactly
#: :data:`USAGE_RECORD_FIELDS` as its keys.
_USAGE_COLUMNS = ', '.join(USAGE_RECORD_FIELDS)

_SCHEMA_MISSING = 'no such table'


def ensure_usage_table(store: Any) -> None:
    """Create the ``usage`` table on ``store``'s database if it is missing.

    ``store`` is a :class:`pipeline.store_sqlite.SqliteStore`, passed rather
    than imported to keep this module free of a cycle. Callers hold the store's
    guard.
    """
    store._conn.execute(USAGE_SCHEMA)


def append_usage_row(
    store: Any, namespace: str, plan_name: str, record: dict
) -> None:
    """Append one usage record to the store's ``usage`` table.

    The record is validated -- and refused -- before any statement runs, so a
    rejected record leaves the table exactly as it was. The insert runs on the
    store's connection, which is how the append joins a transaction the caller
    already holds.
    """
    validate_namespace(namespace)
    validate_plan_name(plan_name)
    values = validate_usage_record(plan_name, record)
    placeholders = ', '.join('?' for _ in USAGE_RECORD_FIELDS)
    statement = (
        f'INSERT INTO usage (namespace, {_USAGE_COLUMNS}) '
        f'VALUES (?, {placeholders})'
    )
    parameters = (namespace, *(values[field] for field in USAGE_RECORD_FIELDS))
    with store._guard:
        ensure_usage_table(store)
        store._conn.execute(statement, parameters)


def read_usage_rows(store: Any, namespace: str, plan_name: str) -> list[dict]:
    """Return ``plan_name``'s usage records in ``namespace``, in id order.

    Each record is a dict whose keys are exactly
    :data:`USAGE_RECORD_FIELDS`, in contract order, so an append and the read
    that follows it agree on the record's shape as well as its values.
    """
    validate_namespace(namespace)
    validate_plan_name(plan_name)
    statement = (
        f'SELECT {_USAGE_COLUMNS} FROM usage '
        'WHERE namespace = ? AND plan = ? ORDER BY id'
    )
    with store._guard:
        try:
            rows = store._conn.execute(statement, (namespace, plan_name)).fetchall()
        except sqlite3.OperationalError as exc:
            if _SCHEMA_MISSING not in str(exc).lower():
                raise
            # No usage was ever appended to this database, so its history is
            # empty. A read must not create the table: a writer in another
            # process may hold the lock.
            return []
    return [dict(zip(USAGE_RECORD_FIELDS, row)) for row in rows]
