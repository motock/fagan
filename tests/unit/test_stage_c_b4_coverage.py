"""Stage C proof that the b4 SqliteAuditLog is held to the shared contracts."""

from __future__ import annotations

import sqlite3

import pytest

from pipeline import audit_contract
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.unit import test_audit_sqlite


def test_should_run_the_audit_log_contract_against_sqlite():
    assert issubclass(
        test_audit_sqlite.TestSqliteAuditLog, audit_contract.AuditLogContract
    )


def test_should_run_the_tamper_contract_against_sqlite():
    assert issubclass(
        test_audit_sqlite.TestSqliteAuditLogTamper, audit_contract.AuditTamperContract
    )


def test_should_run_the_concurrency_contract_against_sqlite():
    assert issubclass(
        test_audit_sqlite.TestSqliteAuditLogConcurrency,
        audit_contract.AuditConcurrencyContract,
    )


def test_should_refuse_an_update_on_an_appended_audit_row():
    log = test_audit_sqlite._make_log()
    log.append(DEFAULT_NAMESPACE, "chain", {"action": "a", "outcome": "ok"})
    conn = sqlite3.connect(test_audit_sqlite._path_of(log))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_records SET record = ?", ("{}",))
    conn.close()
