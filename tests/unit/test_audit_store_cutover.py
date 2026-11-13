"""The audit trail and its anchor go through the configured store.

``pipeline.audit._audit_log()`` returns the process's configured store's
``audit_log()``: the SQLite audit table under ``PIPELINE_STORE=sqlite`` and the
JSONL files under ``PIPELINE_STORE=file``. Under sqlite no plan or host audit
file is written or read; the external anchor stays a file, but it reads chain
heads from the store. A store that cannot serve an audit log refuses the
audited action (fail closed), except for the two failures ``record_audit``
has always degraded: ``OSError`` and ``InvalidRecordKeyError``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import audit, audit_anchor, audit_chain, paths, persistence
from pipeline import store as store_module
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError
from pipeline.store_sqlite import SqliteStore

PLAN = "cutover-plan"
NS = DEFAULT_NAMESPACE
ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"

#: The byte-for-byte line the file store writes for the record below, with the
#: clock and the actor fields pinned by ``pinned_identity``. Unchanged by this
#: story: the file store keeps the legacy layout.
LEGACY_FILE_LINE = (
    '{"action":"merge","actor":"tester","client_id":"client-1",'
    '"correlation_id":"corr-1","detail":{},"hash":"__HASH__",'
    '"outcome":"allowed","prev_hash":"",'
    '"principal_type":"human","seq":0,"target":{"story":"S1"},'
    '"ts":"2026-11-13T00:00:00+00:00"}'
)


class _FixedClock:
    """Stands in for ``datetime`` in :mod:`pipeline.audit` so ``ts`` is fixed."""

    @staticmethod
    def now(tz: Any = None) -> datetime:
        return datetime(2026, 11, 13, tzinfo=UTC)


@pytest.fixture()
def plans_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty plans directory every file-store path would write into."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


@pytest.fixture()
def sqlite_store(plans_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The store ``build_store`` selects under ``PIPELINE_STORE=sqlite``, served
    as the process store the audit facade reads."""
    environ = {
        "PIPELINE_STORE": "sqlite",
        "PIPELINE_STORE_PATH": str(tmp_path / "store.db"),
    }
    built = store_module.build_store(environ)
    assert type(built) is SqliteStore
    monkeypatch.setattr(pipeline.server, "_store", built)
    yield built
    built.close()


@pytest.fixture()
def file_store(plans_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """The store ``build_store`` selects with ``PIPELINE_STORE`` unset."""
    built = store_module.build_store({})
    assert type(built) is store_module.FileStore
    monkeypatch.setattr(pipeline.server, "_store", built)
    return built


@pytest.fixture()
def anchor_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Anchoring to a file outside the plan directory."""
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


def _plan_sidecars(plans_dir: Path) -> list[str]:
    """Every file in the plans directory whose name starts with the plan name."""
    return sorted(p.name for p in plans_dir.iterdir() if p.name.startswith(f"{PLAN}."))


def _audit_files(plans_dir: Path) -> list[str]:
    """Every audit stream file (plan or host) in the plans directory."""
    return sorted(p.name for p in plans_dir.iterdir() if ".audit" in p.name)


def _anchor_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class _StoreWithoutAuditLog:
    """A store that cannot serve an audit log at all."""


class _StoreWhoseAuditLogRaises:
    """A store whose audit log refuses to be built with an unexpected error."""

    def audit_log(self) -> Any:
        raise TypeError("audit log construction is broken")


class _LogRaising:
    """An audit log whose append fails with ``error``."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def append(self, namespace: str, chain: str, body: dict) -> dict:
        raise self._error

    def read_chain(self, namespace: str, chain: str) -> list:
        return []

    def head(self, namespace: str, chain: str) -> None:
        return None

    def list_chains(self, namespace: str) -> list:
        return []


class _StoreWithLog:
    def __init__(self, log: Any) -> None:
        self._log = log

    def audit_log(self) -> Any:
        return self._log


# --------------------------------------------------------------------------
# Outcome 1: under PIPELINE_STORE=sqlite the table is the audit trail
# --------------------------------------------------------------------------


def test_a_sqlite_audit_record_is_written_to_the_table(sqlite_store, plans_dir):
    audit.record_audit(PLAN, action="dispatch", outcome="ok")

    rows = sqlite_store.audit_log().read_chain(NS, PLAN)
    assert [row["action"] for row in rows] == ["dispatch"]


def test_no_plan_or_host_audit_file_is_written_under_sqlite(sqlite_store, plans_dir):
    audit.record_audit(PLAN, action="dispatch", outcome="ok")
    audit.append_host_audit(action="startup", outcome="ok")

    assert _audit_files(plans_dir) == []


def test_verify_audit_reads_the_table_and_reports_a_clean_chain(sqlite_store, plans_dir):
    for action in ("dispatch", "review", "merge"):
        audit.record_audit(PLAN, action=action, outcome="ok")

    assert audit.verify_audit_chain(PLAN) is None
    plans, host = audit.verify_audit_log()
    assert plans == {PLAN: None}
    assert host is None


def test_a_hand_written_audit_file_is_not_read_under_sqlite(sqlite_store, plans_dir):
    # A line that cannot be linked to: were the file read, the chain would
    # break at seq 0. Under sqlite the file is not a source at all.
    (plans_dir / f"{PLAN}.audit.jsonl").write_text("not json\n")

    assert audit.verify_audit_chain(PLAN) is None
    assert sqlite_store.audit_log().read_chain(NS, PLAN) == []
    plans, _host = audit.verify_audit_log()
    assert PLAN not in plans


def test_a_tampered_table_row_is_reported_by_verify_audit(sqlite_store, plans_dir):
    for action in ("dispatch", "review", "merge"):
        audit.record_audit(PLAN, action=action, outcome="ok")
    connection = sqlite_store._conn
    connection.execute("DROP TRIGGER audit_records_no_update")
    stored = json.loads(
        connection.execute(
            "SELECT record FROM audit_records WHERE chain = ? AND seq = 1", (PLAN,)
        ).fetchone()[0]
    )
    stored["action"] = "tampered"
    connection.execute(
        "UPDATE audit_records SET record = ? WHERE chain = ? AND seq = 1",
        (audit_chain.canonical(stored), PLAN),
    )

    assert audit.verify_audit_chain(PLAN) == 1


def test_the_anchor_writes_its_file_and_verifies_a_table_chain(
    sqlite_store, plans_dir, anchor_file
):
    for action in ("dispatch", "review"):
        audit.record_audit(PLAN, action=action, outcome="ok")

    assert audit_anchor.anchor_heads() >= 1
    anchored = [line for line in _anchor_lines(anchor_file) if line["chain"] == PLAN]
    assert [line["seq"] for line in anchored] == [1]
    assert audit_anchor.verify_against_anchor(PLAN) is None


def test_the_anchor_reports_a_table_chain_cut_below_its_anchor(
    sqlite_store, plans_dir, anchor_file
):
    for action in ("dispatch", "review"):
        audit.record_audit(PLAN, action=action, outcome="ok")
    assert audit_anchor.anchor_heads() >= 1
    connection = sqlite_store._conn
    connection.execute("DROP TRIGGER audit_records_no_delete")
    connection.execute("DELETE FROM audit_records WHERE chain = ? AND seq = 1", (PLAN,))

    assert audit_anchor.verify_against_anchor(PLAN) == "truncated below anchored seq 1"


def test_a_sqlite_cycle_leaves_no_plan_file_but_the_anchor(
    sqlite_store, plans_dir, anchor_file
):
    records = sqlite_store.records()
    with records.transaction(NS, PLAN) as acquired:
        assert acquired is True
        records.put_manifest(
            NS, PLAN, {"schema_version": 1, "stories": {"S1": {"status": "pending"}}}
        )
        audit.record_audit(PLAN, action="ingest", outcome="ok")
    records.append_decision(NS, PLAN, {"story": "S1", "decision": "approve"})
    audit.record_audit(PLAN, action="approve", outcome="ok")
    with records.transaction(NS, PLAN) as acquired:
        assert acquired is True
        audit.record_audit(PLAN, action="merge", outcome="allowed")
    assert audit_anchor.anchor_heads() >= 1

    assert _plan_sidecars(plans_dir) == []
    assert audit.verify_audit_chain(PLAN) is None
    assert [line["chain"] for line in _anchor_lines(anchor_file)].count(PLAN) == 1


# --------------------------------------------------------------------------
# Outcome 2: under PIPELINE_STORE=file the bytes are unchanged
# --------------------------------------------------------------------------


def test_the_file_store_writes_the_legacy_chain_bytes(
    file_store, plans_dir, monkeypatch
):
    monkeypatch.setattr(audit, "datetime", _FixedClock)
    monkeypatch.setattr(audit, "actor_id", lambda: "tester")
    monkeypatch.setattr(audit, "client_id", lambda: "client-1")
    monkeypatch.setattr(audit, "principal_type", lambda: "human")
    monkeypatch.setattr(audit, "current_correlation_id", lambda: "corr-1")

    audit.record_audit(PLAN, action="merge", outcome="allowed", target={"story": "S1"})

    written = (plans_dir / f"{PLAN}.audit.jsonl").read_text()
    record = json.loads(written)
    assert written == audit_chain.canonical(record) + "\n"
    assert written == LEGACY_FILE_LINE.replace("__HASH__", record["hash"]) + "\n"


# --------------------------------------------------------------------------
# Fail closed: a store that cannot audit refuses the action
# --------------------------------------------------------------------------


def test_a_store_without_an_audit_log_makes_record_audit_raise(
    monkeypatch, plans_dir
):
    monkeypatch.setattr(pipeline.server, "_store", _StoreWithoutAuditLog())

    with pytest.raises(AttributeError):
        audit.record_audit(PLAN, action="merge", outcome="allowed")

    assert _audit_files(plans_dir) == []


def test_an_unexpected_error_from_the_audit_log_propagates(monkeypatch, plans_dir):
    monkeypatch.setattr(pipeline.server, "_store", _StoreWhoseAuditLogRaises())

    with pytest.raises(TypeError, match="audit log construction is broken"):
        audit.record_audit(PLAN, action="merge", outcome="allowed")


def test_a_database_write_failure_propagates_and_refuses_the_action(monkeypatch, plans_dir):
    # Judgement call flagged for the security reviewer: a sqlite3.Error is the
    # database's equivalent of a full disk, but it is not OSError, so it fails
    # closed rather than degrading like one.
    log = _LogRaising(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(pipeline.server, "_store", _StoreWithLog(log))

    with pytest.raises(sqlite3.OperationalError):
        audit.record_audit(PLAN, action="merge", outcome="allowed")


def test_an_os_error_from_the_audit_log_still_degrades(monkeypatch, plans_dir, caplog):
    log = _LogRaising(OSError(28, "No space left on device"))
    monkeypatch.setattr(pipeline.server, "_store", _StoreWithLog(log))

    audit.record_audit(PLAN, action="merge", outcome="allowed")

    assert "audit append failed action=merge" in caplog.text


def test_an_invalid_chain_name_still_degrades(file_store, plans_dir, caplog):
    audit.record_audit("../escape", action="merge", outcome="allowed")

    assert "audit append refused: invalid chain name action=merge" in caplog.text
    assert list(plans_dir.iterdir()) == []


def test_an_invalid_record_key_error_from_the_log_still_degrades(monkeypatch, plans_dir, caplog):
    log = _LogRaising(InvalidRecordKeyError("refused"))
    monkeypatch.setattr(pipeline.server, "_store", _StoreWithLog(log))

    audit.record_audit(PLAN, action="merge", outcome="allowed")

    assert "audit append refused: invalid chain name action=merge" in caplog.text
