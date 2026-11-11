"""The one-time ``import_file_store`` command (ADR 0012, "no import beyond an explicit tool").

The command reads every record kind through the file store's record view and
writes it through the SQLite store's, for the default namespace, then imports
the audit chains through :func:`pipeline.store_import_audit.import_audit_chains`.

Each test seeds a plans directory through the file store's own API, runs the
command's ``main`` and grades the result: what the database serves afterwards,
the JSON summary on stdout, the exit status, and -- for a dry run -- the raw
rows, read with ``sqlite3`` so that no read-time adoption can write anything.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import pytest

from pipeline import persistence, server
from pipeline.audit_file import SUFFIX, FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE, context_digest
from pipeline.store_sqlite import SqliteStore
from pipeline.tools import import_file_store
from pipeline.tools.import_file_store import main

NS = DEFAULT_NAMESPACE
PLAN = "alpha"
OTHER = "beta"
STORY = "s1"
TS = "2026-11-10T09:00:00+00:00"
CONTENT = "the external context text"

#: The record kinds the summary reports, in the order the command imports them.
KINDS = (
    "manifests",
    "plan_drafts",
    "plan_completions",
    "decisions",
    "journals",
    "usage",
    "outbox",
    "notification_history",
    "context_snapshots",
)

#: Every table the sqlite store writes a record kind to, for raw row counts.
TABLES = (
    "story_manifests",
    "plan_drafts",
    "plan_completions",
    "decisions",
    "journal",
    "usage",
    "outbox",
    "notifications",
    "context_snapshots",
    "audit_records",
)

MANIFEST = {
    "plan_name": PLAN,
    "repo_root": "/srv/repo",
    "stories": {STORY: {"summary": "first", "agent_instructions": "do it"}},
}

USAGE = {
    "ts": TS,
    "plan": PLAN,
    "story_key": STORY,
    "attempt": 1,
    "correlation_id": "c1",
    "role": "dispatch",
    "backend": "claude",
    "model": "sonnet",
    "served_model": None,
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cost_usd": 0.5,
    "duration_ms": 1200,
}


@pytest.fixture
def plans(tmp_path, monkeypatch) -> Path:
    """Point every file-store binding at a fresh plans directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "store.db")


def _seed(plans: Path) -> None:
    """Write one of every record kind, plus an audit chain per plan and the host."""
    records = FileStore().records()
    records.put_manifest(NS, PLAN, MANIFEST)
    records.put_manifest(NS, OTHER, {"plan_name": OTHER, "stories": {}})
    records.put_plan_draft(NS, OTHER, {"plan_name": OTHER, "stories": []})
    records.mark_plan_completed(NS, PLAN)
    records.put_plan_report(NS, PLAN, "the plan is done")
    records.append_decision(NS, PLAN, {"decision": "approve", "ts": TS})
    records.append_journal(NS, PLAN, STORY, {"ts": TS, "event": "step", "step": "one"})
    records.append_usage(NS, PLAN, USAGE)
    records.append_outbox(NS, PLAN, "ops", {"ts": TS, "message": "queued"})
    records.append_notification(NS, PLAN, {"ts": TS, "message": "told", "severity": "info"})
    digest = context_digest(CONTENT)
    records.put_context_snapshot(NS, digest, CONTENT, {"source": "fixture"})
    audit = FileAuditLog()
    audit.append(NS, PLAN, {"action": "plan.approve", "outcome": "allowed"})
    audit.append(NS, HOST_CHAIN_ID, {"action": "host.start", "outcome": "allowed"})


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _kinds(summary: dict) -> dict:
    return summary["record_kinds"]


def _statuses(summary: dict) -> dict[str, str]:
    return {entry["chain"]: entry["status"] for entry in summary["audit"]}


def _raw_counts(db: str) -> dict[str, int]:
    """Count the rows of every table with ``sqlite3``; a missing table is zero."""
    counts: dict[str, int] = {}
    with sqlite3.connect(db) as conn:
        for table in TABLES:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = 0
    return counts


# -- the import ---------------------------------------------------------------
def test_imports_every_record_kind_and_the_audit_chains(plans, db, capsys):
    _seed(plans)

    code, summary = _run(["--db", db], capsys)

    assert code == 0
    assert tuple(_kinds(summary)) == KINDS
    assert all(_kinds(summary)[kind]["refused"] == [] for kind in KINDS)
    store = SqliteStore(db)
    records = store.records()
    file_records = FileStore().records()
    assert records.get_manifest(NS, PLAN) == file_records.get_manifest(NS, PLAN)
    assert records.get_plan_draft(NS, OTHER) == file_records.get_plan_draft(NS, OTHER)
    assert records.is_plan_completed(NS, PLAN) is True
    assert records.get_plan_report(NS, PLAN) == "the plan is done"
    assert records.get_decisions(NS, PLAN) == file_records.get_decisions(NS, PLAN)
    assert records.get_usage(NS, PLAN) == file_records.get_usage(NS, PLAN)
    assert records.get_notification_history(NS, PLAN) == (
        file_records.get_notification_history(NS, PLAN)
    )
    assert records.get_context_snapshot(NS, context_digest(CONTENT))["content"] == CONTENT
    journal = records.get_journal(NS, PLAN, STORY)
    assert [entry["step"] for entry in journal] == ["one"]
    queued = records.read_outbox(NS, PLAN, "ops")
    assert [entry["record"] for entry in queued] == [{"ts": TS, "message": "queued"}]
    store.close()


def test_the_audit_chains_appear_in_the_summary_with_the_import_statuses(plans, db, capsys):
    _seed(plans)

    _, summary = _run(["--db", db], capsys)

    assert _statuses(summary) == {PLAN: "imported", HOST_CHAIN_ID: "imported"}


def test_a_second_run_imports_nothing(plans, db, capsys):
    _seed(plans)
    _run(["--db", db], capsys)

    code, summary = _run(["--db", db], capsys)

    assert code == 0
    for kind in KINDS:
        assert _kinds(summary)[kind]["imported"] == 0, kind
        assert _kinds(summary)[kind]["refused"] == [], kind
    assert _statuses(summary) == {PLAN: "already_present", HOST_CHAIN_ID: "already_present"}


def test_a_refused_audit_chain_exits_non_zero_after_the_rest_is_imported(plans, db, capsys):
    _seed(plans)
    stream = plans / f"{PLAN}{SUFFIX}"
    stream.write_text(stream.read_text().replace('"outcome":"allowed"', '"outcome":"denied"', 1))

    code, summary = _run(["--db", db], capsys)

    assert code == 1
    assert _statuses(summary) == {PLAN: "refused", HOST_CHAIN_ID: "imported"}
    refused = [entry for entry in summary["audit"] if entry["status"] == "refused"]
    assert refused[0]["reason"] == "unverifiable"
    assert _kinds(summary)["manifests"]["imported"] >= 1
    assert _kinds(summary)["usage"]["imported"] == 1


def test_a_dry_run_writes_nothing_and_reports_what_a_real_run_would(plans, db, capsys):
    _seed(plans)

    code, dry = _run(["--db", db, "--dry-run"], capsys)

    assert code == 0
    assert dry["dry_run"] is True
    assert _statuses(dry) == {PLAN: "imported", HOST_CHAIN_ID: "imported"}
    assert all(count == 0 for count in _raw_counts(db).values())

    _, real = _run(["--db", db], capsys)
    assert _kinds(dry) == _kinds(real)
    assert dry["audit"] == real["audit"]


def test_a_relative_db_path_is_refused_before_any_store_is_opened(
    plans, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as refusal:
        main(["--db", "store.db"])

    assert refusal.value.code == 2
    assert not (tmp_path / "store.db").exists()


def test_a_malformed_source_manifest_is_reported_and_the_rest_imports(plans, db, capsys):
    _seed(plans)
    (plans / "broken.manifest.json").write_text("{not json")

    code, summary = _run(["--db", db], capsys)

    assert code == 1
    assert {"name": "broken", "reason": "unreadable"} in _kinds(summary)["manifests"]["refused"]
    assert SqliteStore(db).records().get_manifest(NS, PLAN) == MANIFEST


def test_a_manifest_whose_digest_changes_on_import_is_refused(plans, db, capsys, monkeypatch):
    _seed(plans)
    # Every call answers a new digest, so the before and after reads disagree
    # whatever the manifest holds: the verification must refuse, not trust.
    fresh = itertools.count()
    monkeypatch.setattr(
        import_file_store, "canonical_digest", lambda _m: f"sha256:{next(fresh)}"
    )

    code, summary = _run(["--db", db], capsys)

    assert code == 1
    assert {"name": PLAN, "reason": "digest_mismatch"} in _kinds(summary)["manifests"]["refused"]
