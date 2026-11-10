"""Behaviour and contract tests for the SQLite audit log (ADR 0012 decision 10).

The SQLite audit log is another implementation of the same protocol as the
in-memory one (``pipeline.audit_memory``): append-only, per-namespace,
hash-chained.  It is held to the shared contract mixins in
:mod:`pipeline.audit_contract`, plus the behaviour those mixins cannot state
because it lives in the database itself -- the append-only triggers, the
primary key, and the columns a stored row has to agree with.

The public surface is the ``audit_log()`` accessor of
:class:`pipeline.store_sqlite.SqliteStore`.  Everything below that line --
the triggers, the rows, the columns -- is reached on purpose, because the
properties being graded are properties of the storage.

The tamper seam (``rewrite_raw``) is test-only: it drops the append-only
triggers, rewrites the rows of one chain and puts the triggers back, which is
what anyone with write access to the database file would have to do.  The rows
are written in the order given and each one carries the canonical JSON of the
record it stands for, so a record whose own ``seq`` was altered no longer
agrees with its row and is served as a broken row.
"""

from __future__ import annotations

import atexit
import importlib
import json
import multiprocessing
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from pipeline import audit_chain, audit_contract
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = "plan-a"
BODY = {"action": "story.merge", "outcome": "allowed"}

#: The names the schema gives the append-only guards; they are part of the
#: promised storage, so the tests name them.
UPDATE_TRIGGER = "audit_records_no_update"
DELETE_TRIGGER = "audit_records_no_delete"

#: The phrase both guards refuse with.
APPEND_ONLY = "append-only"

#: The columns of one stored row.
COLUMNS = ("namespace", "chain", "seq", "prev_hash", "record", "hash")

# log -> (the log itself, the store behind it, the database path), so the
# database can be reached without reading a private attribute off the store.
_LOGS: dict[int, tuple[object, SqliteStore, str]] = {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _temp_db_path() -> str:
    """Return the path of a fresh, empty database file."""
    directory = tempfile.mkdtemp(prefix="audit-sqlite-")
    atexit.register(shutil.rmtree, directory, True)
    return str(Path(directory) / "store.db")


def _make_log_at(path: str):
    """Return a fresh audit log over the database at *path*."""
    store = SqliteStore(path)
    log = store.audit_log()
    _LOGS[id(log)] = (log, store, path)
    return log


def _make_log():
    """Return a fresh audit log over a brand-new database."""
    return _make_log_at(_temp_db_path())


def _path_of(log) -> str:
    """Return the database path behind *log*."""
    return _LOGS[id(log)][2]


def _trigger_sql(name: str, event: str) -> str:
    """Return the DDL of one append-only guard."""
    return (
        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {event} ON audit_records "
        "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;"
    )


def _drop_guards(conn: sqlite3.Connection) -> None:
    """Drop both append-only guards (test-only)."""
    conn.execute(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER}")
    conn.execute(f"DROP TRIGGER IF EXISTS {DELETE_TRIGGER}")


def _create_guards(conn: sqlite3.Connection) -> None:
    """Put both append-only guards back (test-only)."""
    conn.execute(_trigger_sql(UPDATE_TRIGGER, "UPDATE"))
    conn.execute(_trigger_sql(DELETE_TRIGGER, "DELETE"))


def _rewrite_rows(log, namespace: str, chain: str, entries) -> None:
    """Replace the stored rows of one chain with *entries* (test-only seam).

    The rows are written in the order given, each one holding the canonical
    JSON of the record it stands for.  A row's ``seq`` column is the position
    of the row in the chain, so a record whose own ``seq`` was altered no
    longer agrees with its row and is served as a broken row.
    """
    conn = sqlite3.connect(_path_of(log), isolation_level=None)
    try:
        _drop_guards(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM audit_records WHERE namespace = ? AND chain = ?",
            (namespace, chain),
        )
        for position, entry in enumerate(entries):
            if entry is None:
                record_text, prev_hash, digest = json.dumps(None), "", ""
            else:
                record_text = audit_chain.canonical(entry)
                prev_hash = str(entry.get("prev_hash", ""))
                digest = str(entry.get("hash", ""))
            conn.execute(
                "INSERT INTO audit_records (namespace, chain, seq, prev_hash, record, hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, chain, position, prev_hash, record_text, digest),
            )
        conn.execute("COMMIT")
    finally:
        _create_guards(conn)
        conn.close()


def _rows(path: str) -> list[tuple]:
    """Return every audit row of the database at *path*, by column name."""
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(f"SELECT {', '.join(COLUMNS)} FROM audit_records"))
    finally:
        conn.close()


def _spawn_writer(db_path: str, chain: str, count: int, barrier) -> None:
    """Append *count* records to *chain* in a child process."""
    store = SqliteStore(db_path)
    log = store.audit_log()
    barrier.wait(timeout=120)
    for index in range(count):
        body = {"action": "spawn", "outcome": "ok", "index": index}
        for _attempt in range(500):
            try:
                log.append(DEFAULT_NAMESPACE, chain, body)
                break
            except sqlite3.OperationalError as exc:
                # The store's busy timeout is short by design, so a writer
                # that lost the race for the database's write lock waits and
                # tries again, exactly as a caller of the store's own
                # transaction API has to.
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                time.sleep(0.01)
        else:
            raise AssertionError("an append never acquired the write lock")


# --------------------------------------------------------------------------
# The shared contract, run against the SQLite audit log
# --------------------------------------------------------------------------


class _SqliteAuditLog:
    """A fresh SQLite-backed audit log for every test."""

    def make_log(self):
        return _make_log()

    def rewrite_raw(self, log, namespace, chain, entries):
        _rewrite_rows(log, namespace, chain, entries)


class TestSqliteAuditLog(
    _SqliteAuditLog,
    audit_contract.AuditLogContract,
    audit_contract.MultiNamespaceContract,
):
    """The shared audit-log contract, run against the SQLite audit log."""


class TestSqliteAuditLogTamper(_SqliteAuditLog, audit_contract.AuditTamperContract):
    """The tamper-detection contract, run against the SQLite audit log."""


class TestSqliteAuditLogConcurrency(
    _SqliteAuditLog, audit_contract.AuditConcurrencyContract
):
    """The concurrency contract, run against the SQLite audit log."""


# --------------------------------------------------------------------------
# The storage itself
# --------------------------------------------------------------------------


def test_the_audit_module_is_part_of_the_package(tmp_path):
    """The SQLite audit log lives in its own module."""
    assert importlib.util.find_spec("pipeline.store_sqlite_audit") is not None


def test_the_audit_log_the_store_serves_is_the_sqlite_one(tmp_path):
    """The accessor serves the log of the store's own database."""
    path = str(tmp_path / "store.db")
    store = SqliteStore(path)
    log = store.audit_log()
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path)
    try:
        count = next(iter(conn.execute("SELECT COUNT(*) FROM audit_records")))[0]
    finally:
        conn.close()
    assert count == 1


def test_the_audit_table_and_its_guards_are_created(tmp_path):
    path = str(tmp_path / "store.db")
    _make_log_at(path)
    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_records)")}
        guards = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    finally:
        conn.close()
    assert set(COLUMNS) <= columns
    assert {UPDATE_TRIGGER, DELETE_TRIGGER} <= guards


def test_a_stored_row_carries_the_canonical_json_of_its_record(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    record = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    rows = _rows(path)
    assert len(rows) == 1
    namespace, chain, seq, prev_hash, record_text, digest = rows[0]
    assert (namespace, chain, seq) == (DEFAULT_NAMESPACE, PLAN, 0)
    assert record_text == audit_chain.canonical(record)
    assert json.loads(record_text) == record
    assert prev_hash == record["prev_hash"]
    assert digest == record["hash"]


def test_an_update_of_a_stored_record_is_refused(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            conn.execute(
                "UPDATE audit_records SET record = ? WHERE namespace = ? AND chain = ?",
                ('{"tampered": true}', DEFAULT_NAMESPACE, PLAN),
            )
    finally:
        conn.close()
    assert APPEND_ONLY in str(caught.value)
    assert log.read_chain(DEFAULT_NAMESPACE, PLAN)[0]["action"] == BODY["action"]


def test_a_delete_of_a_stored_record_is_refused(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            conn.execute(
                "DELETE FROM audit_records WHERE namespace = ? AND chain = ?",
                (DEFAULT_NAMESPACE, PLAN),
            )
    finally:
        conn.close()
    assert APPEND_ONLY in str(caught.value)
    assert len(log.read_chain(DEFAULT_NAMESPACE, PLAN)) == 1


def test_a_duplicate_sequence_is_refused_by_the_primary_key(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_records (namespace, chain, seq, prev_hash, record, hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (DEFAULT_NAMESPACE, PLAN, 0, "", "{}", "0" * 64),
            )
    finally:
        conn.close()
    assert len(log.read_chain(DEFAULT_NAMESPACE, PLAN)) == 1


@pytest.mark.parametrize("column", ["seq", "prev_hash", "hash"])
def test_a_row_whose_columns_disagree_with_its_record_reads_as_none(tmp_path, column):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _drop_guards(conn)
        value = 7 if column == "seq" else "0" * 64
        conn.execute(
            f"UPDATE audit_records SET {column} = ? WHERE namespace = ? AND chain = ?",
            (value, DEFAULT_NAMESPACE, PLAN),
        )
        _create_guards(conn)
    finally:
        conn.close()
    assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == [None]


def test_a_row_whose_record_is_not_a_json_object_reads_as_none(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _drop_guards(conn)
        conn.execute(
            "UPDATE audit_records SET record = ? WHERE namespace = ? AND chain = ?",
            ('["not", "an", "object"]', DEFAULT_NAMESPACE, PLAN),
        )
        _create_guards(conn)
    finally:
        conn.close()
    assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == [None]


def test_an_append_links_to_the_newest_stored_row(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    hand_written = audit_chain.link(None, {"action": "hand", "outcome": "ok"})
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO audit_records (namespace, chain, seq, prev_hash, record, hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                DEFAULT_NAMESPACE,
                PLAN,
                hand_written["seq"],
                hand_written["prev_hash"],
                audit_chain.canonical(hand_written),
                hand_written["hash"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    appended = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    assert appended["seq"] == hand_written["seq"] + 1
    assert appended["prev_hash"] == hand_written["hash"]
    entries = log.read_chain(DEFAULT_NAMESPACE, PLAN)
    assert [entry["seq"] for entry in entries] == [0, 1]
    assert audit_chain.verify_entries(entries) is None


def test_an_append_that_hits_a_unique_constraint_raises_and_leaves_the_chain_alone(
    tmp_path,
):
    """A refused append must raise, must not retry for ever, must write nothing."""
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(
            "CREATE TRIGGER audit_records_force_unique BEFORE INSERT ON audit_records "
            f"WHEN NEW.namespace = '{DEFAULT_NAMESPACE}' AND NEW.chain = '{PLAN}' "
            "BEGIN SELECT RAISE(ABORT, 'UNIQUE constraint failed: "
            "audit_records.namespace, audit_records.chain, audit_records.seq'); END;"
        )
    finally:
        conn.close()

    outcome: list[BaseException] = []

    def worker() -> None:
        try:
            log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        except BaseException as error:  # noqa: BLE001 - reported below
            outcome.append(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), "the append retried without end"
    assert outcome, "the append did not raise"
    assert len(log.read_chain(DEFAULT_NAMESPACE, PLAN)) == 1


def test_a_body_carrying_sql_is_stored_literally(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    payload = "'; DROP TABLE audit_records; --"
    log.append(DEFAULT_NAMESPACE, PLAN, {"action": payload})
    assert log.read_chain(DEFAULT_NAMESPACE, PLAN)[0]["action"] == payload
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "audit_records" in tables


def test_two_processes_appending_to_one_chain_agree_on_the_sequence(tmp_path):
    path = str(tmp_path / "store.db")
    log = _make_log_at(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(target=_spawn_writer, args=(path, PLAN, 50, barrier))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(180)
    for process in processes:
        assert not process.is_alive(), "a writer hung"
        assert process.exitcode == 0, "a writer failed"
    entries = log.read_chain(DEFAULT_NAMESPACE, PLAN)
    assert len(entries) == 100
    assert [entry["seq"] for entry in entries] == list(range(100))
    assert audit_chain.verify_entries(entries) is None
