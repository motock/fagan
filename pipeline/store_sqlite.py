"""``SqliteStore``: the single-host database ``Store`` (ADR 0008 stage 2a).

The database is the only record: manifests as JSON in a ``TEXT`` column, the
per-channel outbox, journals, decisions and every other record as rows of its
tables. The store keeps no file store and writes no plan file; the one-time
import tool (``pipeline.tools.import_file_store``) is the only path from files
into the database. ``build_store`` selects it with ``PIPELINE_STORE`` and
``PIPELINE_STORE_PATH``.

Because the outbox is a table written on the store's own connection, an append
made inside :meth:`SqliteStore.transaction` joins the open ``BEGIN IMMEDIATE``
and commits or rolls back with the manifest row that produced it.

Opening a database is a configuration decision, so it validates its inputs
before touching disk: a relative path would resolve against whatever directory
each process happened to start in, so two processes that believe they share a
store would silently write two different databases. The refusal names the
setting and never echoes the rejected value, which may identify an operator's
home directory.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pipeline import (
    store_sqlite_audit,
    store_sqlite_notifications,
    store_sqlite_outbox,
    store_sqlite_records,
    store_sqlite_schema,
    store_sqlite_usage,
)
from pipeline.concurrency import PLAN_LOCK_RETRY_INTERVAL_SECONDS
from pipeline.store_contract_outbox import OutboxContract, OutboxSummaryContract
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    UnreadableRecordError,
    context_address,
    snapshot_meta,
    validate_channel,
    validate_namespace,
    validate_plan_name,
    validate_story_key,
    verify_context_digest,
)

# How long ``transaction()`` keeps retrying a contended write lock before it
# gives up and yields ``False``. Bounded on purpose: the ``Store`` contract is
# non-blocking (a contended acquire is refused, not waited on), so a caller that
# must wait belongs in its own retry loop where it can log the decision.
BUSY_TIMEOUT_MS = 250

# SQLite's own busy handler sleeps for growing intervals, so the timeout it is
# given is a floor rather than a ceiling: asking for 250ms was measured blocking
# for over a second on a contended database. Each attempt therefore uses a short
# handler and the loop below owns the real deadline, which keeps a refusal
# inside BUSY_TIMEOUT_MS however the handler happens to back off.
_BUSY_ATTEMPT_TIMEOUT_MS = 10
_BUSY_RETRY_S = _BUSY_ATTEMPT_TIMEOUT_MS / 1000


def _is_lock_conflict(exc: sqlite3.OperationalError) -> bool:
    """True when a statement lost the race for the database's write lock."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        message = str(exc).lower()
        return "locked" in message or "busy" in message
    return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

class SqliteStore(
    store_sqlite_records.RecordTablesMixin,
    store_sqlite_notifications.NotificationHistoryMixin,
):
    """A database-backed ``Store`` for a single host.

    The database is the record for plan manifests: each one is stored as JSON
    (``json.dumps`` with sorted keys) in the ``story_manifests`` table, written
    and read through parameterized statements only. ``transaction()`` opens a
    ``BEGIN IMMEDIATE`` write transaction with a bounded busy timeout and yields
    whether it was acquired, so a contended writer is refused rather than
    blocked forever.

    A plan file a writer leaves in the plans directory is not a record of this
    store: it is never read, adopted or rewritten. Only the one-time import
    tool (``python -m pipeline.tools.import_file_store``) copies file-store data
    into the database.

    The workspace reads -- the story log and the worktree files -- are not
    store methods at all: they go through the ``WorkspaceReader`` seam
    (``pipeline/workspace_reader.py``, ADR 0012 decision 1), so no
    control-plane method here returns or reads a filesystem path.

    The audit chain is a table in the database (``audit_records``, served by
    :meth:`audit_log`), but nothing writes the pipeline's audit history through
    it yet: the facade still appends to the ``<plan>.audit.jsonl`` sidecar
    files in the plans directory, and that move is a later story.
    """

    def __init__(self, path: str):
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("PIPELINE_STORE_PATH must be an absolute path")
        self._path = target
        # One connection serves the store, so the guard makes it safe to use
        # from several threads (the MCP server and the dashboard both do) and
        # doubles as the in-process write gate -- SQLite has one writer per
        # database, so a store-wide gate matches what the database allows.
        self._guard = threading.RLock()
        # isolation_level=None puts the driver in autocommit mode: the store
        # issues BEGIN/COMMIT itself, so a transaction boundary is one explicit
        # statement instead of driver-implied behaviour.
        self._conn = sqlite3.connect(
            str(target),
            isolation_level=None,
            timeout=_BUSY_ATTEMPT_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        with self._guard:
            self._conn.execute(f"PRAGMA busy_timeout={_BUSY_ATTEMPT_TIMEOUT_MS}")
            # WAL lets a plain reader in another process (the dashboard) read a
            # consistent snapshot while this store holds the write lock, so
            # serving a recorded row never waits for a writer. In-process callers
            # serialize on ``self._guard`` for the length of a transaction,
            # which matches SQLite allowing one writer per database. The mode
            # must be set outside any transaction.
            self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            # The schema is versioned and migrated in one transaction here, so
            # a database written by a newer build is refused (never opened and
            # written to) and one without a version is adopted as version 1.
            # A rollback therefore takes the whole migration with it.
            try:
                store_sqlite_schema.migrate(self._conn)
            except BaseException:
                # A refused or failed migration must not leave the database's
                # file and its write lock held by a store that will never serve.
                self._conn.close()
                raise
        # Journals, decisions, drafts, completion records and snapshots are
        # rows of this store's database, so a write made inside a transaction
        # is part of it and commits or rolls back with the manifest row.
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the connection. Safe to call once per store."""
        with self._guard:
            self._conn.close()

    def records(self) -> SqliteRecords:
        """ADR 0012 namespaced record view"""
        return SqliteRecords(self)

    def audit_log(self) -> store_sqlite_audit.SqliteAuditLog:
        """Return the audit log held in this store's database.

        The log is a view over the store's own connection, so an append made
        inside :meth:`transaction` joins the transaction the store holds; a
        log keeps no state of its own, so a fresh one per call is the same log.
        """
        return store_sqlite_audit.SqliteAuditLog(self)

    # -- manifests: the database is the record -----------------------------
    def manifest_path(self, plan_name: str) -> Path:
        """Refuse: this store keeps no manifest file to name.

        The manifest is a row of the database. Callers read and write it through
        ``get_manifest`` and ``save_manifest``; ``manifest_path`` is removed from
        the Store Protocol in a later story.
        """
        raise NotImplementedError(
            "SqliteStore keeps manifests in its database, not in a plan file; "
            "use get_manifest / save_manifest"
        )

    def _stored_document(self, plan_name: str) -> dict[str, Any] | None:
        """Return the database row's document, or ``None`` when it has none."""
        with self._guard:
            row = self._conn.execute(
                "SELECT manifest_json FROM story_manifests WHERE plan_name = ?",
                (plan_name,),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    def _write_row(self, plan_name: str, document: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO story_manifests (plan_name, manifest_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(plan_name) DO UPDATE "
            "SET manifest_json = excluded.manifest_json, "
            "updated_at = excluded.updated_at",
            (plan_name, json.dumps(document, sort_keys=True), time.time()),
        )

    def _manifest_updated_at(self, plan_name: str) -> float | None:
        """Return when the plan's row was last written, or ``None`` without one."""
        with self._guard:
            row = self._conn.execute(
                "SELECT updated_at FROM story_manifests WHERE plan_name = ?",
                (plan_name,),
            ).fetchone()
        return None if row is None else row[0]

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        """Return the plan's manifest, failing when the database has no row.

        A stored row that does not parse raises ``JSONDecodeError``: the row was
        damaged outside this store, and the reader must not report a document
        it cannot read.
        """
        document = self._stored_document(plan_name)
        if document is None:
            raise FileNotFoundError(f"no manifest for plan {plan_name!r}")
        return document

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        with self._guard:
            self._write_row(plan_name, manifest)

    def list_manifests(self) -> list[str]:
        """List the plans the database records."""
        with self._guard:
            rows = self._conn.execute(
                "SELECT plan_name FROM story_manifests ORDER BY plan_name"
            ).fetchall()
        return [row[0] for row in rows]

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        manifest = self.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return None
        story.update(fields)
        self.save_manifest(plan_name, manifest)
        return story

    def list_plans(self) -> list[str]:
        """List the plans with a manifest or a staged plan draft, sorted."""
        return sorted(
            set(self.list_manifests()) | set(self._draft_plans(DEFAULT_NAMESPACE))
        )

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        """Fail-open manifest read, served from the same record as the strict one.

        The dashboard's manifest reads go through here, so it must agree with
        ``get_manifest`` about which plans exist. It fails open only where the
        stored document is not valid JSON text, because those callers treat an
        unreadable manifest as "no manifest".
        """
        try:
            return self._stored_document(plan_name)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # -- transactions ------------------------------------------------------
    def _begin_immediate(self) -> bool:
        """Start ``BEGIN IMMEDIATE`` within ``BUSY_TIMEOUT_MS``, or refuse.

        SQLite's busy handler treats its timeout as a floor rather than a
        ceiling, so the loop owns the deadline: each attempt waits only
        ``_BUSY_ATTEMPT_TIMEOUT_MS`` and the store stops retrying once
        ``BUSY_TIMEOUT_MS`` has passed, which keeps a refusal bounded.

        A refused attempt must leave the connection out of a transaction --
        every later acquire from this store depends on a clean connection, and a
        half-open one fails with "cannot start a transaction within a
        transaction" instead of reporting a refusal.
        """
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return True
            except sqlite3.OperationalError as exc:
                if self._conn.in_transaction:
                    # A ROLLBACK that fails must not replace the error that
                    # caused it: the caller's decision is taken on `exc`, so
                    # the rollback failure is contained here.
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                if not _is_lock_conflict(exc):
                    raise
                if time.monotonic() >= deadline:
                    return False
            time.sleep(_BUSY_RETRY_S)

    @contextlib.contextmanager
    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        """Acquire this plan's write transaction, or report it was refused.

        Yields ``True`` when the transaction was acquired and ``False`` when it
        could not be: another thread of this process holds it, or another
        process held the database's write lock for longer than
        ``BUSY_TIMEOUT_MS``. Callers must skip all work on ``False``. The write
        is committed when the block exits normally and rolled back when it
        raises. Re-entering from inside the block acquires the transaction
        already held, so nested call sites compose instead of deadlocking
        against themselves.

        ``wait_seconds`` mirrors ``pipeline.store.Store.transaction``: the
        default ``0.0`` is exactly one attempt with no sleeping (every
        scheduler-internal caller). A positive budget retries the in-process
        guard every ``PLAN_LOCK_RETRY_INTERVAL_SECONDS`` until the deadline
        passes, then yields ``False``. The waiter holds nothing while it
        sleeps, so two waiters cannot deadlock each other.
        """
        if wait_seconds < 0 or not math.isfinite(wait_seconds):
            wait_seconds = 0.0
        deadline = time.monotonic() + wait_seconds
        while not self._guard.acquire(blocking=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield False
                return
            time.sleep(min(PLAN_LOCK_RETRY_INTERVAL_SECONDS, remaining))
        try:
            if self._depth:
                # Re-entered on the holding thread: the outer block owns the
                # transaction's BEGIN and COMMIT.
                self._depth += 1
                try:
                    yield True
                finally:
                    self._depth -= 1
                return
            acquired = self._begin_immediate()
            if not acquired:
                yield False
                return
            self._depth = 1
            try:
                yield True
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self._conn.execute("COMMIT")
        finally:
            self._guard.release()

    # get_recent_workspaces / add_recent_workspace / get_active_workspace /
    # set_active_workspace moved to pipeline.host_state (ADR 0012, decision 4):
    # host-local records, never namespaced and never replicated.


class SqliteRecords(
    store_sqlite_notifications.NotificationHistoryMixin,
    OutboxContract,
    OutboxSummaryContract,
):
    """The namespaced record view of a :class:`SqliteStore` (ADR 0012 stage A).

    Every operation is a view of a legacy ``SqliteStore`` call, so the record
    API and the legacy API can never disagree about what is stored. Stage A
    serves the single namespace ``'default'``: any other valid namespace is
    refused before the store is touched, and an invalid key is rejected before
    anything is written.
    """

    #: The outbox queues are rows in the store's own database, so a write made
    #: inside ``transaction()`` commits or rolls back with the manifest row
    #: (ADR 0012 decision 9).
    atomic_transactions = True

    def __init__(self, store: SqliteStore):
        self._store = store

    def _namespace(self, namespace: str) -> None:
        """Validate ``namespace`` and refuse anything but ``'default'``."""
        validate_namespace(namespace)
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError('unknown namespace')

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        """Return the plan's manifest, or ``None`` when there is none."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store.get_manifest_or_none(plan_name)

    def get_manifest_updated_at(self, namespace: str, plan_name: str) -> float | None:
        """Return when the plan's manifest was last written, or ``None``.

        No manifest row is ``None``, and the stamp is the row's own
        ``updated_at``.
        """
        self._namespace(namespace)
        validate_plan_name(plan_name)
        if self._store.get_manifest_or_none(plan_name) is None:
            return None
        return self._store._manifest_updated_at(plan_name)

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        """Return every manifest recorded in ``namespace``, keyed by plan name."""
        self._namespace(namespace)
        manifests: dict[str, dict] = {}
        for plan_name in self.list_plans(namespace):
            manifest = self.get_manifest(namespace, plan_name)
            if manifest is not None:
                manifests[plan_name] = manifest
        return dict(sorted(manifests.items()))

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        """Store ``manifest`` as the whole document for ``plan_name``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(manifest, dict):
            raise TypeError('manifest must be a dict')
        # Serialise before touching storage: a manifest that cannot be stored
        # must leave no trace behind.
        json.dumps(manifest)
        self._store.save_manifest(plan_name, manifest)

    def list_plans(self, namespace: str) -> list[str]:
        """Return the sorted plan names the store records."""
        self._namespace(namespace)
        return self._store.list_manifests()

    # -- plan drafts (ADR 0012 decision 3) ------------------------------
    # The draft ``save_plan`` stages and ingest refuses with is a row of this
    # store's ``plan_drafts`` table, written and read through parameterized
    # statements only, so nothing lands in the plans directory.

    def put_plan_draft(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Stage ``plan`` as the transient plan draft of ``plan_name``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(plan, dict):
            raise TypeError('plan must be a dict')
        # Serialise before touching storage: a document that cannot be stored
        # must leave no trace behind.
        json.dumps(plan)
        self._store._put_draft_row(namespace, plan_name, plan)

    def get_plan_draft(self, namespace: str, plan_name: str) -> dict | None:
        """Return ``plan_name``'s plan draft, or ``None`` when it has none.

        A draft this store holds itself answers from its table.  A draft it does
        not hold is read from the plans directory through
        :func:`pipeline.persistence.read_plan_file` -- the reader ``save_plan``
        stages it for and ingest refuses with -- so a plan staged by a writer
        that still writes the file is never lost.  That fallback only reads: a
        draft this store writes is a row, and nothing is ever staged on disk.
        """
        self._namespace(namespace)
        validate_plan_name(plan_name)
        payload = self._store._draft_payload(namespace, plan_name)
        if payload is not None:
            return json.loads(payload)
        from pipeline import persistence

        path = persistence._journal_path(plan_name, 'story').parent / (
            f'{plan_name}.json'
        )
        if not path.is_file():
            return None
        plan, refusal = persistence.read_plan_file(path)
        if refusal is not None:
            raise UnreadableRecordError(refusal['error'])
        return plan

    def delete_plan_draft(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s plan draft; return whether one was deleted."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store._delete_draft_row(namespace, plan_name)

    def list_plan_drafts(self, namespace: str) -> list[str]:
        """Return the sorted plan names that have an ingestable draft."""
        self._namespace(namespace)
        return self._store._draft_plans(namespace)

    # -- plan completion marker and report (ADR 0012 decision 3) --------
    # The once-only marker and the report are columns of one row of this
    # store's ``plan_completions`` table, so neither is a file in the plans
    # directory any more.
    def mark_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Create ``plan_name``'s completion marker; True only if created."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store._mark_completed_row(
            namespace, plan_name, store_sqlite_records.completion_timestamp()
        )

    def is_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Whether ``plan_name``'s completion marker exists."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store._completed_at(namespace, plan_name) is not None

    def put_plan_report(self, namespace: str, plan_name: str, text: str) -> None:
        """Store ``plan_name``'s report, replacing the previous one wholesale."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(text, str):
            raise TypeError('plan report must be a str')
        self._store._put_report_row(namespace, plan_name, text)

    def get_plan_report(self, namespace: str, plan_name: str) -> str | None:
        """Return ``plan_name``'s report, or ``None`` when it has none."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store._report_text(namespace, plan_name)

    # -- context snapshots (ADR 0012 decision 11) -----------------------
    # A snapshot is content-addressed, so it is a row of this store's
    # ``context_snapshots`` table keyed by ``(namespace, digest)``: written
    # once with an insert-or-ignore and never updated, because the digest
    # names the content the row holds.
    def put_context_snapshot(
        self, namespace: str, digest: str, content: str, meta: dict
    ) -> None:
        """Store the snapshot ``digest`` addresses, once and for all."""
        self._namespace(namespace)
        hexdigest = verify_context_digest(digest, content)
        payload = snapshot_meta(meta, digest, content)
        self._store._put_snapshot_row(
            namespace, 'sha256:' + hexdigest, content, payload
        )

    def get_context_snapshot(self, namespace: str, digest: str) -> dict | None:
        """Return ``{'content', 'meta'}`` for ``digest``, or ``None``."""
        self._namespace(namespace)
        hexdigest = context_address(digest)
        stored = self._store._snapshot_row(namespace, 'sha256:' + hexdigest)
        if stored is None:
            stored = self._store._snapshot_row(namespace, digest)
        return stored

    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        """Acquire this plan's write transaction, or report it was refused.

        Validates the keys and refuses any namespace but
        :data:`DEFAULT_NAMESPACE` before anything is locked, then delegates to
        :meth:`SqliteStore.transaction`: ``BEGIN IMMEDIATE``, the store-wide
        in-process guard and re-entrance all live there.  This method adds no
        locking of its own.

        Yields ``True`` when the transaction was acquired and ``False`` when it
        was not; callers must skip all work on ``False``.
        """
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store.transaction(plan_name, wait_seconds)

    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]:
        """Return the decision records for ``plan_name``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return [
            json.loads(payload)
            for payload in self._store._decision_payloads(namespace, plan_name)
        ]

    def append_decision(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one decision record to ``plan_name``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        self._store.append_decision(plan_name, record)

    # -- usage records -------------------------------------------------
    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        """Return the plan's usage records from the ``usage`` table, in id order.

        Each record's keys are exactly :data:`USAGE_RECORD_FIELDS`. A database
        that never recorded usage serves an empty history: the read never
        creates the table.
        """
        self._namespace(namespace)
        return store_sqlite_usage.read_usage_rows(self._store, namespace, plan_name)

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one usage record to the ``usage`` table.

        The record is validated before any SQL runs, so a rejected record
        leaves the table unchanged; the insert runs on the store's connection,
        so an append made inside :meth:`transaction` joins that transaction and
        rolls back with it.
        """
        self._namespace(namespace)
        store_sqlite_usage.append_usage_row(self._store, namespace, plan_name, record)

    # The notification history is the store's ``notifications`` table, served
    # by ``NotificationHistoryMixin`` (pipeline/store_sqlite_notifications.py):
    # a row written here inside ``transaction()`` commits or rolls back with
    # the manifest row, and the store's legacy readers serve the same table
    # (ADR 0012 decision 9).

    def get_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> list[dict]:
        """Return a story's journal entries, in the order they were written.

        The file store normalizes every entry with the optional progress fields
        (``step``, ``summary``, ``next_hint``) present and ``None`` when the
        writer gave none; the record view omits the fields a record never had,
        so a record read back is the record that was written.
        """
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        # The record view omits the display-only fields the writer never set,
        # so a record read back is the record that was written; the defaults
        # are applied on the way out and never written back to the row.
        entries = [
            json.loads(payload)
            for payload in self._store._journal_payloads(
                namespace, plan_name, story_key
            )
        ]
        return [
            {
                key: value
                for key, value in entry.items()
                if value is not None
                or key not in store_sqlite_records.JOURNAL_OPTIONAL_FIELDS
            }
            for entry in entries
        ]

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        """Append one journal record for ``story_key``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        self._store.append_journal(plan_name, story_key, record)

    def clear_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> None:
        """Delete a story's journal rows, which are the store's own."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        self._store.clear_journal(plan_name, story_key)

    # -- journal activity ----------------------------------------------
    def get_journal_updated_at(
        self, namespace: str, plan_name: str, story_key: str
    ) -> float | None:
        """Return when the story's journal was last appended to, or ``None``.

        The activity is read from the rows themselves -- the last append's
        stamp -- so a journal that was cleared has no activity, exactly as a
        deleted sidecar had none.
        """
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        return self._store._journal_updated_at(namespace, plan_name, story_key)

    def list_journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        """Return the sorted story keys that have a journal in ``plan_name``."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        return self._store._journal_keys(namespace, plan_name)
    # -- per-channel outbox queues (ADR 0012 decisions 3 and 9) ----------
    # The queues are rows in the store's own ``outbox`` table, written on the
    # store's single connection. Inside ``transaction()`` the write joins the
    # open ``BEGIN IMMEDIATE``, so an append commits or rolls back with the
    # manifest row that produced it; outside one it is its own statement.
    def append_outbox(
        self, namespace: str, plan_name: str, channel: str, record: dict
    ) -> None:
        """Queue ``record`` on the ``(plan_name, channel)`` outbox table."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        with self._store._guard:
            store_sqlite_outbox.append(
                self._store._conn, namespace, plan_name, channel, record
            )

    def read_outbox(
        self,
        namespace: str,
        plan_name: str,
        channel: str,
        limit: int | None = None,
    ) -> list[dict]:
        """Return the queue's entries oldest first, without removing them."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        with self._store._guard:
            return store_sqlite_outbox.read(
                self._store._conn, namespace, plan_name, channel, limit
            )

    def ack_outbox(
        self, namespace: str, plan_name: str, channel: str, ids
    ) -> int:
        """Acknowledge ``ids``: remove one pending entry per id, return the count."""
        self._namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        with self._store._guard:
            return store_sqlite_outbox.ack(
                self._store._conn, namespace, plan_name, channel, ids
            )

    def outbox_summary(self, namespace: str, plan_name=None) -> dict:
        """Summarise the outbox rows, counting without carrying a payload."""
        self._namespace(namespace)
        if plan_name is not None:
            try:
                validate_plan_name(plan_name)
            except InvalidRecordKeyError:
                return {'files': 0, 'records': 0, 'oldest_age_s': None}
        with self._store._guard:
            return store_sqlite_outbox.summary(
                self._store._conn, namespace, plan_name
            )

    def list_outboxes(self, namespace: str) -> list[tuple[str, str]]:
        """Return the sorted ``(plan, channel)`` pairs that hold entries."""
        self._namespace(namespace)
        with self._store._guard:
            return store_sqlite_outbox.list_queues(self._store._conn, namespace)
