"""The audit chain, end to end: append, anchor, tamper, verify.

This is the acceptance test for ADR 0012 decision 10 as an operator meets it:
real callers append, the scheduler's anchor pass publishes the heads, an
attacker edits the files, and the ``verify_audit`` tool reports exactly where.

Nothing here reaches into a private attribute or writes a record by hand. The
records are written by the callers that write them in production --
``pipeline.ownership`` (a plan-scoped chain) and ``pipeline.authorization``'s
host-scoped path (the host chain) -- the heads are published by
``pipeline.audit_anchor.anchor_heads``, and the verdict is read from the
``verify_audit`` tool an operator actually calls.

The chains and the anchor log live in a temp plan directory and a temp anchor
log outside it, so nothing here touches the operator's real streams.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle; see test_audit_anchor
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import (
    audit_anchor,
    authorization,
    ownership,
    persistence,
    watchers,
)
from pipeline import server_tools_lifecycle as lifecycle
from pipeline.actor import bind_principal
from pipeline.audit_chain import verify_entries
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore
from pipeline.store_sqlite_audit import ensure_schema

ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"
PLAN = "alpha"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit and the scan's plan listing at a temp plan directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr("pipeline.server.PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    return directory


@pytest.fixture()
def anchor_file(tmp_path: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable anchoring to an append-only log outside ``PLAN_DIR``."""
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


def _append_plan_records(plan_name: str, count: int) -> None:
    """Append *count* records to a plan chain through the ownership path.

    Every call changes the plan's owner, which is the change
    ``pipeline.ownership`` audits, so the chain is written by the caller that
    writes it in production rather than by the test.
    """
    prior = {"owner": "alice", "delegates": []}
    for index in range(count):
        prior = ownership.resolve_ownership(
            plan_name, {"owner": f"owner{index}"}, prior, LOCAL_OPERATOR.subject
        )


def _host_denial() -> dict | None:
    """Ask for a host-scoped write as an agent: refused, and audited.

    An agent is structurally refused ``config:write`` (ADR 0014 decision 3), so
    the refusal does not depend on the role rules, and the host chain records
    the denied decision.
    """
    agent = Principal(subject="agent-1", principal_type="agent", team_scope="acme/team-1")
    with bind_principal(agent):
        return authorization.refusal_for_host_config(
            "config:write", "the global role defaults"
        )


def _plan_stream(plan_dir: Path, plan_name: str = PLAN) -> Path:
    return plan_dir / f"{plan_name}.audit.jsonl"


def _host_stream(plan_dir: Path) -> Path:
    return plan_dir / ".audit.host.jsonl"


def _anchor_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _edit_line(path: Path, index: int) -> None:
    """Rewrite one stored record's ``outcome``, leaving its recorded hash."""
    lines = path.read_text().splitlines()
    record = json.loads(lines[index])
    record["outcome"] = "tampered"
    lines[index] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")


def _drop_lines(path: Path, keep: int) -> None:
    """Rewrite *path* with only its first *keep* lines - the tail cut."""
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:keep]))


class _Bus:
    """The event bus the sweep publishes to."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, event: dict) -> None:
        self.published.append(event)


# --------------------------------------------------------------------------
# The whole chain, intact
# --------------------------------------------------------------------------


def test_intact_chain_reports_no_tamper_anywhere(
    plan_dir: Path, anchor_file: Path
) -> None:
    """Real callers append, the anchor pass publishes, the tool finds nothing."""
    _append_plan_records(PLAN, 3)
    refusal = _host_denial()
    assert refusal is not None
    assert refusal["ok"] is False

    assert audit_anchor.anchor_heads() == 2
    assert sorted(line["chain"] for line in _anchor_lines(anchor_file)) == [
        PLAN,
        "host",
    ]

    report = lifecycle.verify_audit()

    assert report["ok"] is True
    assert report["plans"] == {PLAN: None}
    assert report["host"] is None
    assert report["anchor"] == {PLAN: None, "host": None}


# --------------------------------------------------------------------------
# Tampering with the chains
# --------------------------------------------------------------------------


def test_edited_record_is_reported_with_its_seq(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A rewritten record is named by its seq; the anchored head still holds."""
    _append_plan_records(PLAN, 3)
    audit_anchor.anchor_heads()

    _edit_line(_plan_stream(plan_dir), 1)

    report = lifecycle.verify_audit()

    assert report["plans"] == {PLAN: 1}
    assert report["anchor"][PLAN] is None


def test_cut_tail_is_invisible_to_the_chain_but_not_to_the_anchor(
    plan_dir: Path, anchor_file: Path
) -> None:
    """The chain alone cannot see a cut tail; the anchored head can."""
    _append_plan_records(PLAN, 3)
    audit_anchor.anchor_heads()

    _drop_lines(_plan_stream(plan_dir), keep=1)

    report = lifecycle.verify_audit()

    assert report["plans"] == {PLAN: None}
    assert report["anchor"][PLAN] == "truncated below anchored seq 2"

    # A further anchor pass must not lower the anchor to the cut head, which
    # would launder the truncation on the next tick.
    assert audit_anchor.anchor_heads() == 0
    assert lifecycle.verify_audit()["anchor"][PLAN] == "truncated below anchored seq 2"


def test_deleted_host_chain_is_reported_as_truncated(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A host chain deleted outright is reported, not silently dropped."""
    _append_plan_records(PLAN, 1)
    _host_denial()
    audit_anchor.anchor_heads()

    _host_stream(plan_dir).unlink()

    report = lifecycle.verify_audit()

    assert report["anchor"]["host"] == "truncated below anchored seq 0"
    assert report["host"] is None


# --------------------------------------------------------------------------
# The anchor log itself
# --------------------------------------------------------------------------


def test_unreadable_anchor_log_reports_no_chain_as_clean(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A log that cannot be read is a verdict on every chain, not a clean one."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits, so the log stays readable")
    _append_plan_records(PLAN, 1)
    assert audit_anchor.anchor_heads() == 1
    assert anchor_file.exists()
    size = anchor_file.stat().st_size
    anchor_file.chmod(0o000)
    try:
        report = lifecycle.verify_audit()
        appended = audit_anchor.anchor_heads()
    finally:
        anchor_file.chmod(0o600)

    assert report["anchor"] == {"*": "anchor log unreadable: PermissionError"}
    assert appended == 0
    assert anchor_file.stat().st_size == size


def test_second_anchor_pass_with_no_new_records_appends_nothing(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A quiet stream does not grow the anchor log."""
    _append_plan_records(PLAN, 1)

    assert audit_anchor.anchor_heads() == 1
    lines = len(_anchor_lines(anchor_file))

    assert audit_anchor.anchor_heads() == 0
    assert len(_anchor_lines(anchor_file)) == lines


def test_a_refusing_sink_raises_and_does_not_stop_the_sweep(
    plan_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sink that refuses the append fails the pass, not the daemon."""
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{link}")
    _append_plan_records(PLAN, 1)

    with pytest.raises(OSError):
        audit_anchor.anchor_heads()

    assert target.read_text() == ""

    worktree = plan_dir / f"{PLAN}-wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"exit_code": 0}))
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps(
            {
                "plan": PLAN,
                "stories": {"S-1": {"status": "in_progress", "worktree": str(worktree)}},
            }
        )
    )

    with caplog.at_level(logging.WARNING):
        events = watchers.scan_all_plans(_Bus())

    assert events
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == watchers.logger.name
    ]
    assert warnings
    assert any("OSError" in record.getMessage() for record in warnings)


# --------------------------------------------------------------------------
# The same chain, stored in the table
# --------------------------------------------------------------------------


def _sqlite_log(tmp_path: Path) -> tuple[object, str]:
    """Return a SQLite audit log over a fresh database, and its path."""
    database = str(tmp_path / "store.db")
    log = SqliteStore(database).audit_log()
    for index in range(3):
        log.append(
            DEFAULT_NAMESPACE,
            PLAN,
            {"action": "plan:ownership_change", "outcome": f"allowed-{index}"},
        )
    return log, database


def _rewrite_row(database: str, statement: str, value: str) -> None:
    """Run *statement* against the chain's row 1, with the guards dropped.

    Dropping the append-only guards is what anyone with write access to the
    database file has to do; they are put back afterwards.
    """
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        for (name,) in list(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_records'"
            )
        ):
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(
            statement,
            (value, DEFAULT_NAMESPACE, PLAN),
        )
        ensure_schema(connection)
    finally:
        connection.close()


def test_the_table_detects_an_edited_row_at_the_same_seq(tmp_path: Path) -> None:
    """The SQLite audit log detects the same edit, at the same position."""
    log, database = _sqlite_log(tmp_path)

    row = sqlite3.connect(database).execute(
        "SELECT record FROM audit_records WHERE namespace = ? AND chain = ? AND seq = 1",
        (DEFAULT_NAMESPACE, PLAN),
    ).fetchone()
    record = json.loads(row[0])
    record["outcome"] = "tampered"
    _rewrite_row(
        database,
        "UPDATE audit_records SET record = ? WHERE namespace = ? AND chain = ? AND seq = 1",
        json.dumps(record, sort_keys=True, separators=(",", ":")),
    )

    entries = log.read_chain(DEFAULT_NAMESPACE, PLAN)

    assert verify_entries(entries) == 1


def test_the_table_rejects_a_row_that_contradicts_itself(tmp_path: Path) -> None:
    """A row whose hash column disagrees with its record is not served."""
    log, database = _sqlite_log(tmp_path)

    _rewrite_row(
        database,
        "UPDATE audit_records SET hash = ? WHERE namespace = ? AND chain = ? AND seq = 1",
        "0" * 64,
    )

    entries = log.read_chain(DEFAULT_NAMESPACE, PLAN)

    assert entries[1] is None
    assert verify_entries(entries) == 1
