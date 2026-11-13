"""The record tables of the ``SqliteStore`` database (ADR 0012 stage C).

The decisions and the checkpoint journals, and then the plan drafts, the
completion records and the context snapshots, are tables of the store's own
database rather than sidecar files in the plans directory.  Every statement
here is parameterized: a record is stored as data, never as part of a
statement.

The tables are created by migrations 2 and 3 of
:mod:`pipeline.store_sqlite_schema`, which owns the ladder and the table
shapes; this module holds only the row operations over them.  The row helpers live here rather than in
``pipeline/store_sqlite.py`` so the store stays the store's behaviour and the
module stays inside the line-limit gate.

The row helpers are mixed into :class:`pipeline.store_sqlite.SqliteStore`, which
supplies the connection, the lock and the transaction depth: a write made inside
the store's ``transaction()`` joins that transaction, and one made outside it
runs in a ``BEGIN IMMEDIATE`` transaction of its own, so the sequence number a
row is given and the row itself are one step.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import Any

from pipeline.store_records import DEFAULT_NAMESPACE

#: The optional progress fields the legacy journal reader fills in.  The record
#: view omits them when the writer never set them; the ``Store`` API, whose
#: readers expect the keys, serves them as ``None``.
JOURNAL_OPTIONAL_FIELDS = ("step", "summary", "next_hint")


def completion_timestamp() -> str:
    """Return the moment a plan is completed, as an ISO 8601 UTC stamp."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class RecordTablesMixin:
    """The store's decisions, journals, drafts, completions and snapshots.

    The host class supplies ``_guard`` (its re-entrant write lock), ``_depth``
    (how many ``transaction()`` blocks it holds) and ``_begin_immediate()``,
    which opens the store's own write transaction or reports it was refused.
    """

    _guard: Any
    _depth: int
    _conn: sqlite3.Connection

    def _begin_immediate(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    @contextlib.contextmanager
    def _write_records(self):
        """Hold the write transaction for a record write, or join the open one."""
        with self._guard:
            if self._depth:
                yield
                return
            if not self._begin_immediate():
                raise sqlite3.OperationalError('the store is busy')
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    def _write(self, sql: str, params: tuple) -> int:
        """Run one write statement, in the open transaction or its own."""
        with self._write_records():
            return self._conn.execute(sql, params).rowcount

    def _record_row(self, sql: str, params: tuple) -> tuple | None:
        """Run one read statement and return its first row, or ``None``."""
        with self._guard:
            return self._conn.execute(sql, params).fetchone()

    def _record_rows(self, sql: str, params: tuple) -> list[tuple]:
        """Run one read statement and return every row."""
        with self._guard:
            return self._conn.execute(sql, params).fetchall()

    # -- decisions -----------------------------------------------------
    def _append_decision_row(
        self, namespace: str, plan_name: str, record: dict[str, Any]
    ) -> None:
        """Append one decision row, numbered within ``plan_name``."""
        self._write(
            'INSERT INTO decisions (namespace, plan, seq, record_json) '
            'SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ? FROM decisions '
            'WHERE namespace = ? AND plan = ?',
            (namespace, plan_name, json.dumps(record), namespace, plan_name),
        )

    def _decision_payloads(self, namespace: str, plan_name: str) -> list[str]:
        """Return the decision rows of ``plan_name`` in append order."""
        return [
            row[0]
            for row in self._record_rows(
                'SELECT record_json FROM decisions WHERE namespace = ? '
                'AND plan = ? ORDER BY seq',
                (namespace, plan_name),
            )
        ]

    # -- journals ------------------------------------------------------
    def _append_journal_row(
        self,
        namespace: str,
        plan_name: str,
        story_key: str,
        record: dict[str, Any],
    ) -> None:
        """Append one journal row, numbered within the story's journal."""
        self._write(
            'INSERT INTO journal (namespace, plan, story_key, seq, record_json, '
            'created_at) SELECT ?, ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ? '
            'FROM journal WHERE namespace = ? AND plan = ? AND story_key = ?',
            (namespace, plan_name, story_key, json.dumps(record), time.time(),
             namespace, plan_name, story_key),
        )

    def _journal_payloads(
        self, namespace: str, plan_name: str, story_key: str
    ) -> list[str]:
        """Return the journal rows of ``story_key`` in append order."""
        return [
            row[0]
            for row in self._record_rows(
                'SELECT record_json FROM journal WHERE namespace = ? '
                'AND plan = ? AND story_key = ? ORDER BY seq',
                (namespace, plan_name, story_key),
            )
        ]

    def _clear_journal_rows(
        self, namespace: str, plan_name: str, story_key: str
    ) -> None:
        """Delete every row of ``story_key``'s journal."""
        self._write(
            'DELETE FROM journal WHERE namespace = ? AND plan = ? '
            'AND story_key = ?',
            (namespace, plan_name, story_key),
        )

    def _journal_updated_at(
        self, namespace: str, plan_name: str, story_key: str
    ) -> float | None:
        """Return when ``story_key``'s journal was last appended to."""
        row = self._record_row(
            'SELECT MAX(created_at) FROM journal WHERE namespace = ? '
            'AND plan = ? AND story_key = ?',
            (namespace, plan_name, story_key),
        )
        return None if row is None or row[0] is None else float(row[0])

    def _journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        """Return the sorted story keys that have a journal in ``plan_name``."""
        return sorted(
            row[0]
            for row in self._record_rows(
                'SELECT DISTINCT story_key FROM journal WHERE namespace = ? '
                'AND plan = ?',
                (namespace, plan_name),
            )
        )

    def _final_journal_payload(
        self, namespace: str, plan_name: str, story_key: str
    ) -> str | None:
        """Return the last journal row of ``story_key``, or ``None``."""
        row = self._record_row(
            'SELECT record_json FROM journal WHERE namespace = ? AND plan = ? '
            'AND story_key = ? ORDER BY seq DESC LIMIT 1',
            (namespace, plan_name, story_key),
        )
        return None if row is None else row[0]

    # -- plan drafts ---------------------------------------------------
    def _put_draft_row(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Store ``plan`` as ``plan_name``'s whole draft document."""
        self._write(
            'INSERT INTO plan_drafts (namespace, plan, plan_json) '
            'VALUES (?, ?, ?) ON CONFLICT(namespace, plan) '
            'DO UPDATE SET plan_json = excluded.plan_json',
            (namespace, plan_name, json.dumps(plan)),
        )

    def _draft_payload(self, namespace: str, plan_name: str) -> str | None:
        """Return the stored draft JSON, or ``None`` when there is none."""
        row = self._record_row(
            'SELECT plan_json FROM plan_drafts WHERE namespace = ? AND plan = ?',
            (namespace, plan_name),
        )
        return None if row is None else row[0]

    def _delete_draft_row(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s draft; True only if a row was deleted."""
        return self._write(
            'DELETE FROM plan_drafts WHERE namespace = ? AND plan = ?',
            (namespace, plan_name),
        ) == 1

    def _draft_plans(self, namespace: str) -> list[str]:
        """Return the sorted plan names that have a draft."""
        return sorted(
            row[0]
            for row in self._record_rows(
                'SELECT plan FROM plan_drafts WHERE namespace = ?', (namespace,)
            )
        )

    # -- plan completion -----------------------------------------------
    def _mark_completed_row(
        self, namespace: str, plan_name: str, completed_at: str
    ) -> bool:
        """Create ``plan_name``'s completion marker; True only if created.

        The update only takes an unmarked row and the insert only lands when
        there is no row at all; both run in one write transaction, so two
        racing callers cannot both create the marker.
        """
        with self._write_records():
            updated = self._conn.execute(
                'UPDATE plan_completions SET completed_at = ? WHERE namespace '
                '= ? AND plan = ? AND completed_at IS NULL',
                (completed_at, namespace, plan_name),
            )
            if updated.rowcount == 1:
                return True
            inserted = self._conn.execute(
                'INSERT OR IGNORE INTO plan_completions '
                '(namespace, plan, completed_at, report_text) '
                'VALUES (?, ?, ?, NULL)',
                (namespace, plan_name, completed_at),
            )
            return inserted.rowcount == 1

    def _completed_at(self, namespace: str, plan_name: str) -> str | None:
        """Return when ``plan_name`` was completed, or ``None``."""
        row = self._record_row(
            'SELECT completed_at FROM plan_completions '
            'WHERE namespace = ? AND plan = ?',
            (namespace, plan_name),
        )
        return None if row is None else row[0]

    def _put_report_row(self, namespace: str, plan_name: str, text: str) -> None:
        """Store ``plan_name``'s report, replacing the previous one."""
        self._write(
            'INSERT INTO plan_completions (namespace, plan, completed_at, '
            'report_text) VALUES (?, ?, NULL, ?) ON CONFLICT(namespace, plan) '
            'DO UPDATE SET report_text = excluded.report_text',
            (namespace, plan_name, text),
        )

    def _report_text(self, namespace: str, plan_name: str) -> str | None:
        """Return ``plan_name``'s report, or ``None`` when it has none."""
        row = self._record_row(
            'SELECT report_text FROM plan_completions '
            'WHERE namespace = ? AND plan = ?',
            (namespace, plan_name),
        )
        return None if row is None else row[0]

    # -- context snapshots ---------------------------------------------
    def _put_snapshot_row(
        self, namespace: str, digest: str, content: str, meta: dict
    ) -> None:
        """Store the snapshot ``digest`` addresses, once and for all."""
        self._write(
            'INSERT OR IGNORE INTO context_snapshots '
            '(namespace, digest, content, meta_json) VALUES (?, ?, ?, ?)',
            (namespace, digest, content, json.dumps(meta)),
        )

    def _snapshot_row(self, namespace: str, digest: str) -> dict | None:
        """Return ``{'content', 'meta'}`` for ``digest``, or ``None``."""
        row = self._record_row(
            'SELECT content, meta_json FROM context_snapshots '
            'WHERE namespace = ? AND digest = ?',
            (namespace, digest),
        )
        if row is None:
            return None
        return {'content': row[0], 'meta': json.loads(row[1])}

    # -- security-audit state ------------------------------------------
    def _put_audit_row(self, namespace: str, repo_key: str, state_json: str) -> None:
        """Store ``repo_key``'s audit row, replacing the previous one."""
        self._write(
            'INSERT INTO security_audit_state (namespace, repo_key, state_json) '
            'VALUES (?, ?, ?) ON CONFLICT(namespace, repo_key) '
            'DO UPDATE SET state_json = excluded.state_json',
            (namespace, repo_key, state_json),
        )

    def _audit_row(self, namespace: str, repo_key: str) -> dict | None:
        """Return ``repo_key``'s recorded audit, or ``None`` when it has none."""
        row = self._record_row(
            'SELECT state_json FROM security_audit_state '
            'WHERE namespace = ? AND repo_key = ?',
            (namespace, repo_key),
        )
        return None if row is None else json.loads(row[0])

    # -- the legacy ``Store`` API over the same rows --------------------
    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None:
        self._append_decision_row(DEFAULT_NAMESPACE, plan_name, record)

    def get_decisions(self, plan_name: str) -> list[dict]:
        return [
            json.loads(payload)
            for payload in self._decision_payloads(DEFAULT_NAMESPACE, plan_name)
        ]

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None:
        self._append_journal_row(DEFAULT_NAMESPACE, plan_name, story_key, record)

    def clear_journal(self, plan_name: str, story_key: str) -> None:
        """Delete a story's journal rows."""
        self._clear_journal_rows(DEFAULT_NAMESPACE, plan_name, story_key)

    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]:
        """Return ``(available, entries)`` with the display defaults filled in."""
        entries = [
            json.loads(payload)
            for payload in self._journal_payloads(
                DEFAULT_NAMESPACE, plan_name, story_key
            )
        ]
        if not entries:
            return False, []
        return True, [
            {
                **entry,
                **{
                    name: entry.get(name)
                    for name in JOURNAL_OPTIONAL_FIELDS
                    if name not in entry
                },
            }
            for entry in entries
        ]

    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None:
        payload = self._final_journal_payload(
            DEFAULT_NAMESPACE, plan_name, story_key
        )
        return None if payload is None else json.loads(payload).get('ts')
