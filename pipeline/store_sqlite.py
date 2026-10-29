"""``SqliteStore``: the single-host database ``Store`` (ADR 0008 stage 2a).

The database holds the store's records -- manifests as JSON in a ``TEXT``
column -- while everything that is natively a filesystem object is delegated to
an internal file store. ``build_store`` selects it with ``PIPELINE_STORE`` and
``PIPELINE_STORE_PATH``.

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
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pipeline.store import build_store

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

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS story_manifests ("
    "plan_name TEXT PRIMARY KEY, "
    "manifest_json TEXT NOT NULL"
    ")"
)


class SqliteStore:
    """A database-backed ``Store`` for a single host.

    The database is the record for plan manifests: each one is stored as JSON
    (``json.dumps`` with sorted keys) in the ``story_manifests`` table, written
    and read through parameterized statements only. ``transaction()`` opens a
    ``BEGIN IMMEDIATE`` write transaction with a bounded busy timeout and yields
    whether it was acquired, so a contended writer is refused rather than
    blocked forever.

    The pipeline also writes each manifest to its plan's manifest file, and
    some writers write that file without going through this store. The
    database therefore reconciles on read: a projection the database has not
    recorded -- or that differs from its row -- is imported before it is
    served, so a plan authored by such a writer is never lost and the next save
    cannot write a stale row back over the file.

    Reads that are natively filesystem objects are delegated to an internal
    file store: ``get_story_log``, ``get_worktree_file``, the recent and active
    workspace records, and the manifest's on-disk projection
    (``manifest_path``, refreshed when a save commits).

    The notification, audit and journal sidecar files remain on disk under
    ``PLAN_DIR`` and are not represented in the database. ``SqliteStore``
    performs no migration from file-store data: plans that are already on disk
    are adopted as they are read, and nothing else is imported.
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
            # WAL lets a reader in another process (the dashboard) proceed while
            # this store holds the database's write lock; in-process callers
            # serialize on ``self._guard`` for the length of a transaction,
            # which matches SQLite allowing one writer per database. The mode
            # must be set outside any transaction.
            self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self._conn.execute(_SCHEMA)
        # The filesystem half is always the file store, whatever PIPELINE_STORE
        # selects for this process.
        self._files = build_store({"PIPELINE_STORE": "file"})
        # Manifests written inside a transaction are projected to disk when it
        # commits, so a rolled-back transaction leaves no trace on disk.
        self._pending_projection: dict[str, dict[str, Any]] = {}
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the connection. Safe to call once per store."""
        with self._guard:
            self._conn.close()

    # -- manifests: the database is the record -----------------------------
    def manifest_path(self, plan_name: str) -> Path:
        return self._files.manifest_path(plan_name)

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
            "INSERT INTO story_manifests (plan_name, manifest_json) VALUES (?, ?) "
            "ON CONFLICT(plan_name) DO UPDATE "
            "SET manifest_json = excluded.manifest_json",
            (plan_name, json.dumps(document, sort_keys=True)),
        )

    def _projected_document(self, plan_name: str) -> dict[str, Any] | None:
        """Return the manifest's projection, or ``None`` when there is none.

        A projection that exists but does not parse raises ``JSONDecodeError``:
        the document was damaged outside this store, and neither reader may
        report a record the filesystem contradicts.
        """
        try:
            return self._files.get_manifest(plan_name)
        except FileNotFoundError:
            return None

    def _resolved_document(self, plan_name: str) -> dict[str, Any] | None:
        """Return the manifest this store serves, reconciling the two records.

        Writers that do not go through this store update only the projection,
        so the projection wins when it differs from the database row and is
        imported, which keeps ``get_manifest``, ``get_manifest_or_none`` and
        ``list_manifests`` reporting one deployment. With no projection the
        row is complete on its own. ``None`` means neither record exists.

        A manifest that this store saved inside the current transaction is the
        exception: its row is newer than the file, which is only projected when
        the transaction commits, so importing the file there would silently
        revert the caller's own write.
        """
        with self._guard:
            # A damaged projection raises here, before either reader can serve
            # a record the filesystem contradicts.
            projected = self._projected_document(plan_name)
            stored = self._stored_document(plan_name)
            if plan_name in self._pending_projection or projected is None:
                return stored
            if projected != stored:
                self._write_row(plan_name, projected)
            return projected

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        """Return the plan's manifest, failing when neither record has it."""
        document = self._resolved_document(plan_name)
        if document is None:
            raise FileNotFoundError(f"no manifest for plan {plan_name!r}")
        return document

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        with self._guard:
            self._write_row(plan_name, manifest)
            if self._conn.in_transaction:
                self._pending_projection[plan_name] = manifest
            else:
                self._files.save_manifest(plan_name, manifest)

    def list_manifests(self) -> list[str]:
        """List every plan either record knows, so an ingest is never lost."""
        with self._guard:
            rows = self._conn.execute(
                "SELECT plan_name FROM story_manifests ORDER BY plan_name"
            ).fetchall()
        recorded = {row[0] for row in rows}
        return sorted(recorded | set(self._files.list_manifests()))

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

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        """Fail-open manifest read, served from the same records as the strict one.

        The dashboard's manifest reads go through here, so it must agree with
        ``get_manifest`` about which plans exist: a plan the database records is
        present even when its projection was never written. It fails open only
        where a document is not safely readable -- no row and no projection, or
        a stored document or projection that does not parse -- because those
        callers treat an unreadable manifest as "no manifest".
        """
        try:
            return self._resolved_document(plan_name)
        except json.JSONDecodeError:
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
    def transaction(self, plan_name: str):
        """Acquire this plan's write transaction, or report it was refused.

        Yields ``True`` when the transaction was acquired and ``False`` when it
        could not be: another thread of this process holds it, or another
        process held the database's write lock for longer than
        ``BUSY_TIMEOUT_MS``. Callers must skip all work on ``False``. The write
        is committed when the block exits normally and rolled back when it
        raises. Re-entering from inside the block acquires the transaction
        already held, so nested call sites compose instead of deadlocking
        against themselves.
        """
        if not self._guard.acquire(blocking=False):
            yield False
            return
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
                self._pending_projection.clear()
                self._conn.execute("ROLLBACK")
                raise
            else:
                pending, self._pending_projection = self._pending_projection, {}
                self._depth = 0
                self._conn.execute("COMMIT")
                for plan, manifest in pending.items():
                    self._files.save_manifest(plan, manifest)
        finally:
            self._guard.release()

    # -- delegations to the file store ------------------------------------
    def list_plans(self) -> list[str]:
        return self._files.list_plans()

    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None:
        self._files.append_decision(plan_name, record)

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None:
        self._files.append_journal(plan_name, story_key, record)

    def get_notifications(self, plan_name: str) -> list[str]:
        return self._files.get_notifications(plan_name)

    def get_notification_records(self, plan_name: str, limit: int = 100) -> list[dict]:
        return self._files.get_notification_records(plan_name, limit)

    def get_decisions(self, plan_name: str) -> list[dict]:
        return self._files.get_decisions(plan_name)

    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]:
        return self._files.get_journal(plan_name, story_key)

    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None:
        return self._files.get_journal_final_ts(plan_name, story_key)

    def get_story_log(
        self,
        plan_name: str,
        story_key: str,
        manifest: dict[str, Any],
        lines: int = 200,
    ) -> dict[str, Any]:
        return self._files.get_story_log(plan_name, story_key, manifest, lines)

    def get_worktree_file(
        self, story: dict[str, Any], filename: str
    ) -> dict[str, Any]:
        return self._files.get_worktree_file(story, filename)

    def get_recent_workspaces(self) -> list[str]:
        return self._files.get_recent_workspaces()

    def add_recent_workspace(self, path: str) -> None:
        self._files.add_recent_workspace(path)

    def get_active_workspace(self) -> str | None:
        return self._files.get_active_workspace()

    def set_active_workspace(self, path: str | None) -> None:
        self._files.set_active_workspace(path)
