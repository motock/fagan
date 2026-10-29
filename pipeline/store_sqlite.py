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

    Reads that are natively filesystem objects are delegated to an internal
    file store: ``get_story_log``, ``get_worktree_file``, the recent and active
    workspace records, and the manifest's on-disk projection
    (``manifest_path``, refreshed when a save commits).

    The notification, audit and journal sidecar files remain on disk under
    ``PLAN_DIR`` and are not represented in the database. ``SqliteStore``
    performs no migration from file-store data: selecting it starts a
    deployment from the plans it records from then on.
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
            # WAL lets a reader (the dashboard) proceed while a writer holds the
            # write lock, and must be set outside any transaction.
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

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        """Return the plan's manifest from the database record.

        Every save also projects the manifest to the plan's manifest file, which
        is what the dashboard and external tooling read. A projection that no
        longer parses means the document was damaged outside this store, so the
        read fails closed (``JSONDecodeError``) rather than serving a record the
        filesystem contradicts; a projection that was never written is fine,
        because the record is complete on its own.
        """
        with self._guard:
            row = self._conn.execute(
                "SELECT manifest_json FROM story_manifests WHERE plan_name = ?",
                (plan_name,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"no manifest for plan {plan_name!r}")
            document = json.loads(row[0])
        try:
            self._files.get_manifest(plan_name)
        except FileNotFoundError:
            pass
        return document

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        with self._guard:
            self._conn.execute(
                "INSERT INTO story_manifests (plan_name, manifest_json) VALUES (?, ?) "
                "ON CONFLICT(plan_name) DO UPDATE "
                "SET manifest_json = excluded.manifest_json",
                (plan_name, json.dumps(manifest, sort_keys=True)),
            )
            if self._conn.in_transaction:
                self._pending_projection[plan_name] = manifest
            else:
                self._files.save_manifest(plan_name, manifest)

    def list_manifests(self) -> list[str]:
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

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        """Fail-open manifest read, delegated to the file store's reader.

        It follows the manifest's on-disk projection: a damaged or missing
        projection means the document is not safely readable, and callers of
        this accessor treat that as "no manifest" rather than as an error.
        """
        return self._files.get_manifest_or_none(plan_name)

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
