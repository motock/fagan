"""The ``audit_records`` table of the sqlite store (ADR 0012 decision 10).

The audit trail is the record of what the pipeline decided and did, so the
database has to hold it the way the code holds it: append-only, per namespace
and hash-chained.  This module is the second implementation of
:class:`pipeline.audit_log.AuditLog` -- the in-memory log in
:mod:`pipeline.audit_memory` is the reference -- and it is held to the same
contract suite in :mod:`pipeline.audit_contract`.

The promises are kept by the storage, not only by the code:

* ``(namespace, chain, seq)`` is the primary key, so a chain can never hold two
  records at one position, whatever wrote them;
* two ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers refuse with ``audit records
  are append-only``, so history cannot be rewritten through any connection,
  including one this module never sees;
* the whole record is stored as canonical JSON in ``record``
  (:func:`pipeline.audit_chain.canonical`), and the ``seq``, ``prev_hash`` and
  ``hash`` columns repeat the chain fields.  A read serves a row only while the
  two agree, so a row edited outside this module -- which has to drop the
  triggers first -- is served as ``None`` instead of being believed, repaired or
  quietly skipped.  ``verify_entries`` then reports it at its own position.

What a table with no external anchor still cannot prove is truncation: a writer
who deletes the newest rows leaves a chain that verifies.  That is why the head
hash is anchored outside the database (ADR 0012 decision 7).

The table lives here rather than in :mod:`pipeline.store_sqlite` to keep that
module within its line budget.  An append runs on the store's own connection, so
one made inside ``SqliteStore.transaction`` joins the transaction the store
already holds: a rollback, or a process that dies before the commit, leaves no
row behind.

Every namespace is served: the namespace is part of the row key, not a filter
on one configured tenant, so two namespaces may each hold ``seq`` 0 of a chain
of the same name.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pipeline.audit_chain import canonical, link
from pipeline.audit_log import HOST_CHAIN_ID, validate_chain
from pipeline.store_records import validate_namespace

#: The phrase both append-only guards refuse with.
APPEND_ONLY_MESSAGE = 'audit records are append-only'

#: ``audit_records`` carries the chain key, the chain fields repeated as columns
#: and the record itself as canonical JSON.  The primary key is the chain
#: position of a namespace's chain, so ``seq`` can be neither duplicated nor
#: renumbered behind the code's back.
AUDIT_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS audit_records ('
    'namespace TEXT NOT NULL, '
    'chain TEXT NOT NULL, '
    'seq INTEGER NOT NULL, '
    'prev_hash TEXT NOT NULL, '
    'record TEXT NOT NULL, '
    'hash TEXT NOT NULL, '
    'PRIMARY KEY (namespace, chain, seq)'
    ')'
)

#: ``(name, event)`` of every append-only guard.  They live in the schema rather
#: than only in this module because a guard the writer can skip is not a guard.
AUDIT_TRIGGERS = (
    ('audit_records_no_update', 'UPDATE'),
    ('audit_records_no_delete', 'DELETE'),
)

_TRIGGER_SQL = (
    'CREATE TRIGGER IF NOT EXISTS {name} BEFORE {event} ON audit_records '
    "BEGIN SELECT RAISE(ABORT, '{message}'); END;"
)

#: The columns one stored row is read back from.  ``seq``/``prev_hash``/``hash``
#: are read beside ``record`` because the read is what checks they agree.
_SELECT_ROWS = (
    'SELECT seq, prev_hash, hash, record FROM audit_records '
    'WHERE namespace = ? AND chain = ? ORDER BY seq'
)
_SELECT_HEAD = (
    'SELECT seq, prev_hash, hash, record FROM audit_records '
    'WHERE namespace = ? AND chain = ? ORDER BY seq DESC LIMIT 1'
)
_SELECT_TAIL_FIELDS = (
    'SELECT seq, hash FROM audit_records '
    'WHERE namespace = ? AND chain = ? ORDER BY seq DESC LIMIT 1'
)
_SELECT_CHAINS = 'SELECT DISTINCT chain FROM audit_records WHERE namespace = ?'
_INSERT_ROW = (
    'INSERT INTO audit_records (namespace, chain, seq, prev_hash, record, hash) '
    'VALUES (?, ?, ?, ?, ?, ?)'
)

#: How many times one append may try to claim the next chain position: the
#: first attempt plus at most one retry, which is what a lost race costs.  A
#: longer loop would turn a writer that keeps losing -- or a database that
#: refuses every insert -- into a hang instead of an error.
MAX_APPEND_ATTEMPTS = 2

_MISSING_TABLE = 'no such table'


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the audit table and its append-only guards on *conn*.

    Called at store construction, outside any transaction: a table an append
    inside the first transaction of a brand-new file writes to must not be
    created by that transaction, where a rollback would take it away.  The DDL
    is ``IF NOT EXISTS`` throughout, so opening a database that already has them
    is a no-op and does not touch the rows.
    """
    conn.execute(AUDIT_SCHEMA)
    for name, event in AUDIT_TRIGGERS:
        conn.execute(
            _TRIGGER_SQL.format(name=name, event=event, message=APPEND_ONLY_MESSAGE)
        )


def _decode(seq: int, prev_hash: str, digest: str, record_text: str) -> dict | None:
    """Return the row's record, or ``None`` when the row contradicts itself.

    A row is served only while it is the record it claims to be: the JSON must
    be an object and its own ``seq``, ``prev_hash`` and ``hash`` must equal the
    columns beside it.  Anything else -- an edit made with the guards dropped, a
    row written by hand -- is reported as ``None`` so verification names its
    position.  Nothing is repaired, deleted or skipped: this read has no side
    effects.
    """
    try:
        record = json.loads(record_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get('seq') != seq:
        return None
    if record.get('prev_hash') != prev_hash:
        return None
    if record.get('hash') != digest:
        return None
    return record


def _detached(record: dict) -> dict:
    """Return *record* as the stored row will serve it.

    The round trip through canonical JSON is deliberate: ``append`` returns what
    is in the table rather than an object sharing structure with the caller's
    body, and it deep-copies for free, so a caller that keeps the body cannot
    change stored history through it.
    """
    return json.loads(canonical(record))


class SqliteAuditLog:
    """An append-only, hash-chained audit log in the store's database.

    The store hands one instance to each caller through
    :meth:`pipeline.store_sqlite.SqliteStore.audit_log`; it holds no cache of
    its own, so every append reads the chain's tail from the database and no
    in-memory high-water mark can advance ahead of a commit.
    """

    def __init__(self, store: Any) -> None:
        # The store is passed rather than imported to keep this module free of
        # a cycle; ``_conn`` and ``_guard`` are the store's own connection and
        # its store-wide write gate, so an append joins the store's transaction
        # policy instead of opening a second database path.
        self._store = store

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store._conn

    # -- appending ---------------------------------------------------------
    def append(self, namespace: str, chain: str, body: dict) -> dict:
        """Append *body* to the chain and return the record as stored.

        The record is linked to the newest stored row, so a chain written partly
        by hand still continues correctly.  The read of the tail and the insert
        share one ``BEGIN IMMEDIATE`` transaction, so two writers cannot claim
        the same ``seq``; if one loses the race anyway -- another process
        committed between the read and the insert -- the insert is refused by
        the primary key, the transaction is rolled back and the append is
        retried once from a fresh tail read.  A second refusal raises.

        Argument validation runs before any statement, so a refused append
        leaves the table exactly as it was.
        """
        validate_namespace(namespace)
        validate_chain(chain)
        conn = self._conn
        with self._store._guard:
            if conn.in_transaction:
                # An append inside the store's own transaction joins it: the
                # store already holds the database's write lock and the guard,
                # so nothing can take the position between the read and the
                # insert.  The caller owns the boundary, so this path retries
                # nothing and rolls nothing back.
                record = self._linked(namespace, chain, body)
                self._insert(namespace, chain, record)
                return _detached(record)
            return self._append_owning_transaction(namespace, chain, body)

    def _append_owning_transaction(self, namespace: str, chain: str, body: dict) -> dict:
        """Append in a transaction this call opens, retrying once on a lost race.

        The first attempt plus at most one retry is the whole budget: a writer
        that loses the position again is refused rather than looped, and a
        database that refuses every insert cannot become a hang.
        """
        for attempt in range(1, MAX_APPEND_ATTEMPTS + 1):
            if not self._store._begin_immediate():
                raise sqlite3.OperationalError(
                    'database is locked: audit append could not acquire the '
                    'write lock'
                )
            try:
                record = self._linked(namespace, chain, body)
                self._insert(namespace, chain, record)
                self._conn.execute('COMMIT')
            except sqlite3.IntegrityError:
                # A failed attempt must leave the connection out of a
                # transaction, or every later acquire from this store fails
                # with "cannot start a transaction within a transaction".
                self._rollback()
                if attempt == MAX_APPEND_ATTEMPTS:
                    raise
                continue
            except BaseException:
                self._rollback()
                raise
            return _detached(record)
        raise sqlite3.IntegrityError('audit append could not claim a chain position')

    def _linked(self, namespace: str, chain: str, body: dict) -> dict:
        """Return *body* linked to the chain's newest stored row."""
        return link(self._tail(namespace, chain), dict(body))

    def _tail(self, namespace: str, chain: str) -> dict | None:
        """Return the chain's newest stored position, or ``None`` when empty.

        Only the two chain fields a successor needs are read, and they are read
        from the *columns*: a row whose JSON disagrees with them is still the
        position the chain has reached, so linking to it keeps the numbering of
        the existing rows and leaves the disagreement for verification to
        report.
        """
        try:
            row = self._conn.execute(
                _SELECT_TAIL_FIELDS, (namespace, chain)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _MISSING_TABLE in str(exc):
                return None
            raise
        if row is None:
            return None
        return {'seq': int(row[0]), 'hash': str(row[1])}

    def _insert(self, namespace: str, chain: str, record: dict) -> None:
        """Write one linked record as a row.  Parameterized only."""
        self._conn.execute(
            _INSERT_ROW,
            (
                namespace,
                chain,
                int(record['seq']),
                str(record['prev_hash']),
                canonical(record),
                str(record['hash']),
            ),
        )

    def _rollback(self) -> None:
        """End the open transaction, if any, without masking the caller's error."""
        if not self._conn.in_transaction:
            return
        try:
            self._conn.execute('ROLLBACK')
        except sqlite3.Error:
            # A rollback that fails must not replace the error that caused it:
            # the caller decides on that one.
            pass

    # -- reading -----------------------------------------------------------
    def read_chain(self, namespace: str, chain: str) -> list[dict | None]:
        """Return the chain in storage order, ``[]`` when it does not exist."""
        validate_namespace(namespace)
        validate_chain(chain)
        with self._store._guard:
            rows = self._rows(_SELECT_ROWS, namespace, chain)
        return [_decode(*row) for row in rows]

    def head(self, namespace: str, chain: str) -> dict | None:
        """Return the newest record of the chain, or ``None`` when absent.

        ``None`` is also the answer when the newest row contradicts itself, for
        the same reason it is in :meth:`read_chain`: a broken row is not a
        record that can be reported as the chain's head.
        """
        validate_namespace(namespace)
        validate_chain(chain)
        with self._store._guard:
            rows = self._rows(_SELECT_HEAD, namespace, chain)
        if not rows:
            return None
        return _decode(*rows[0])

    def list_chains(self, namespace: str) -> list[str]:
        """Return the namespace's chains: plan chains sorted, then the host chain.

        The order is the contract's, not the database's, so a caller listing
        chains sees the same sequence whichever audit log served it.
        """
        validate_namespace(namespace)
        with self._store._guard:
            try:
                names = [
                    str(row[0])
                    for row in self._conn.execute(_SELECT_CHAINS, (namespace,))
                ]
            except sqlite3.OperationalError as exc:
                if _MISSING_TABLE in str(exc):
                    return []
                raise
        host = [HOST_CHAIN_ID] if HOST_CHAIN_ID in names else []
        return sorted(name for name in names if name != HOST_CHAIN_ID) + host

    def _rows(self, statement: str, namespace: str, chain: str) -> list[tuple]:
        """Return the rows *statement* selects, tolerating an absent table.

        A database that has never held an audit record has no table to read,
        and an empty chain is the truth about it.  Only a missing table is
        tolerated: every other failure is the caller's to see.
        """
        try:
            return list(self._conn.execute(statement, (namespace, chain)))
        except sqlite3.OperationalError as exc:
            if _MISSING_TABLE in str(exc):
                return []
            raise
