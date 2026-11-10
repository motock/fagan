"""The file-backed audit log over today's JSONL layout (ADR 0012 decision 7).

``pipeline.audit_file.FileAuditLog`` implements the ``AuditLog`` protocol
against the files the pipeline has always written: one ``<plan>.audit.jsonl``
per plan plus the host-scoped ``.audit.host.jsonl``.  No migration, no second
layout: a chain written before the refactor has to verify through the new API
and the bytes the new writer appends next have to be the bytes the old writer
would have appended.

The committed fixtures were generated on ``origin/master`` by
``tests/fixtures/audit/gen_legacy_audit_fixtures.py``, which froze
``pipeline.audit.datetime`` and bound one principal:

    python tests/fixtures/audit/gen_legacy_audit_fixtures.py

They are the compatibility contract, so this module compares against their
bytes rather than recomputing them.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from pipeline import audit, persistence
from pipeline.audit_chain import verify_entries
from pipeline.audit_contract import (
    AuditConcurrencyContract,
    AuditLogContract,
    AuditTamperContract,
    SingleNamespaceContract,
)
from pipeline.audit_file import FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "audit"

#: The plan chain the committed fixtures describe.
LEGACY_PLAN = "legacy-plan"

#: The namespace the file log serves and no other.
_OTHER_NAMESPACE = "acme"

#: The suffix every plan-scoped stream carries.
_SUFFIX = ".audit.jsonl"

#: The body the legacy writer stamped on its last record.
_LEGACY_BODY = {
    "ts": "2026-11-07T11:45:00+00:00",
    "actor": "legacy-fixture",
    "principal_type": "human",
    "client_id": "fixture-client",
    "correlation_id": "fixture-correlation",
    "action": "plan.review",
    "outcome": "allowed",
    "target": {"plan": LEGACY_PLAN},
    "detail": {},
}


@pytest.fixture(autouse=True)
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit streams at a temp plan directory for one test.

    ``pipeline.audit`` reads ``persistence.PLAN_DIR`` at call time, so patching
    the module attribute redirects the file log's streams with it.
    """
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def log() -> FileAuditLog:
    """A file audit log writing into the temp plan directory."""
    return FileAuditLog()


def _legacy_bytes() -> bytes:
    """The committed legacy plan fixture, byte for byte."""
    return (FIXTURES / "legacy.audit.jsonl").read_bytes()


def _legacy_lines() -> list[dict]:
    """The committed legacy plan fixture, parsed."""
    return [json.loads(line) for line in _legacy_bytes().decode().splitlines()]


# --------------------------------------------------------------------------
# Legacy compatibility: the files the old writer produced still verify
# --------------------------------------------------------------------------


def test_a_legacy_plan_chain_verifies_through_the_new_api(log: FileAuditLog) -> None:
    (audit.audit_path(LEGACY_PLAN)).write_bytes(_legacy_bytes())

    entries = log.read_chain(DEFAULT_NAMESPACE, LEGACY_PLAN)

    assert len(entries) == 2
    assert verify_entries(entries) is None


def test_the_head_of_a_legacy_chain_is_its_last_record(log: FileAuditLog) -> None:
    (audit.audit_path(LEGACY_PLAN)).write_bytes(_legacy_bytes())

    head = log.head(DEFAULT_NAMESPACE, LEGACY_PLAN)

    assert head["hash"] == _legacy_lines()[-1]["hash"]
    assert head["seq"] == 1


def test_a_legacy_host_chain_verifies_through_the_new_api(log: FileAuditLog) -> None:
    (audit.host_audit_path()).write_bytes(
        (FIXTURES / "legacy.audit.host.jsonl").read_bytes()
    )

    entries = log.read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)

    assert len(entries) == 1
    assert verify_entries(entries) is None


def test_appending_after_a_legacy_chain_links_to_the_legacy_tail(
    log: FileAuditLog,
) -> None:
    """The new writer must continue the legacy chain, not restart it."""
    legacy = _legacy_bytes()
    audit.audit_path(LEGACY_PLAN).write_bytes(legacy)

    record = log.append(DEFAULT_NAMESPACE, LEGACY_PLAN, dict(_LEGACY_BODY))

    tail = _legacy_lines()[-1]
    assert record["prev_hash"] == tail["hash"]
    assert record["seq"] == tail["seq"] + 1


def test_the_legacy_bytes_are_a_prefix_of_the_new_writers_output(
    log: FileAuditLog,
) -> None:
    legacy = _legacy_bytes()
    audit.audit_path(LEGACY_PLAN).write_bytes(legacy)

    log.append(DEFAULT_NAMESPACE, LEGACY_PLAN, dict(_LEGACY_BODY))

    written = audit.audit_path(LEGACY_PLAN).read_bytes()
    assert written[: len(legacy)] == legacy


def test_the_line_the_new_writer_appends_is_one_canonical_json_line(
    log: FileAuditLog,
) -> None:
    legacy = _legacy_bytes()
    audit.audit_path(LEGACY_PLAN).write_bytes(legacy)

    record = log.append(DEFAULT_NAMESPACE, LEGACY_PLAN, dict(_LEGACY_BODY))

    written = audit.audit_path(LEGACY_PLAN).read_bytes()
    new_line = written[len(legacy) :]
    assert new_line.endswith(b"\n")
    assert json.loads(new_line) == record
    assert new_line.decode().count("\n") == 1


# --------------------------------------------------------------------------
# The file layout the file log owns
# --------------------------------------------------------------------------


def test_the_first_append_creates_a_private_file(log: FileAuditLog) -> None:
    log.append(DEFAULT_NAMESPACE, "p", {"action": "a", "outcome": "ok"})

    mode = audit.audit_path("p").stat().st_mode & 0o777

    assert mode == 0o600


def test_the_plan_chain_lives_at_the_audit_path(log: FileAuditLog) -> None:
    log.append(DEFAULT_NAMESPACE, "p", {"action": "a", "outcome": "ok"})

    assert audit.audit_path("p").exists()
    assert audit.audit_path("p").name == f"p{_SUFFIX}"


def test_the_host_chain_lives_at_the_host_audit_path(log: FileAuditLog) -> None:
    log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, {"action": "a", "outcome": "ok"})

    assert audit.host_audit_path().exists()
    assert audit.host_audit_path().name == ".audit.host.jsonl"


# --------------------------------------------------------------------------
# Namespaces: the file log serves the default namespace and no other
# --------------------------------------------------------------------------


def test_an_unknown_namespace_is_refused_on_append(log: FileAuditLog) -> None:
    with pytest.raises(UnknownNamespaceError):
        log.append(_OTHER_NAMESPACE, "p", {"action": "a", "outcome": "ok"})


def test_an_unknown_namespace_is_refused_on_read(log: FileAuditLog) -> None:
    with pytest.raises(UnknownNamespaceError):
        log.read_chain(_OTHER_NAMESPACE, "p")


def test_an_unknown_namespace_creates_no_file(log: FileAuditLog, plan_dir: Path) -> None:
    with pytest.raises(UnknownNamespaceError):
        log.append(_OTHER_NAMESPACE, "p", {"action": "a", "outcome": "ok"})

    assert list(plan_dir.iterdir()) == []


# --------------------------------------------------------------------------
# Chains: the plan-name rule applies to a chain name
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chain", ["../x", "", "a/b", "host:x", "host"])
def test_an_invalid_chain_is_refused_and_writes_nothing(
    log: FileAuditLog, plan_dir: Path, chain: str
) -> None:
    with pytest.raises(InvalidRecordKeyError):
        log.append(DEFAULT_NAMESPACE, chain, {"action": "a", "outcome": "ok"})

    assert list(plan_dir.iterdir()) == []


# --------------------------------------------------------------------------
# list_chains: what the plan directory holds
# --------------------------------------------------------------------------


def test_list_chains_names_the_plan_chains(log: FileAuditLog) -> None:
    log.append(DEFAULT_NAMESPACE, "zeta", {"action": "a", "outcome": "ok"})
    log.append(DEFAULT_NAMESPACE, "alpha", {"action": "a", "outcome": "ok"})

    assert log.list_chains(DEFAULT_NAMESPACE) == ["alpha", "zeta"]


def test_list_chains_puts_the_host_chain_last(log: FileAuditLog) -> None:
    log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, {"action": "a", "outcome": "ok"})
    log.append(DEFAULT_NAMESPACE, "alpha", {"action": "a", "outcome": "ok"})

    assert log.list_chains(DEFAULT_NAMESPACE) == ["alpha", HOST_CHAIN_ID]


def test_a_redirected_host_path_inside_the_plan_directory_is_not_a_plan_chain(
    log: FileAuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host stream parked under a plan-stream name is not a plan chain."""
    host = audit.audit_path("").parent / f"host{_SUFFIX}"
    monkeypatch.setattr(audit, "host_audit_path", lambda: host)

    log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, {"action": "a", "outcome": "ok"})

    assert log.list_chains(DEFAULT_NAMESPACE) == []


def test_a_stray_host_stream_in_the_plan_directory_is_skipped_with_a_warning(
    log: FileAuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    """The reserved name ``host`` never becomes a chain id."""
    (audit.audit_path("").parent / f"host{_SUFFIX}").write_text("")
    log.append(DEFAULT_NAMESPACE, "alpha", {"action": "a", "outcome": "ok"})

    with caplog.at_level("WARNING"):
        chains = log.list_chains(DEFAULT_NAMESPACE)

    assert chains == ["alpha"]
    assert sum(1 for record in caplog.records if record.levelname == "WARNING") == 1


def test_a_plan_name_with_a_colon_is_skipped_with_a_warning(
    log: FileAuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    (audit.audit_path("").parent / f"odd:name{_SUFFIX}").write_text("")
    log.append(DEFAULT_NAMESPACE, "alpha", {"action": "a", "outcome": "ok"})

    with caplog.at_level("WARNING"):
        chains = log.list_chains(DEFAULT_NAMESPACE)

    assert chains == ["alpha"]
    assert sum(1 for record in caplog.records if record.levelname == "WARNING") == 1


def test_the_warning_names_neither_the_file_nor_the_path(
    log: FileAuditLog, caplog: pytest.LogCaptureFixture
) -> None:
    (audit.audit_path("").parent / f"host{_SUFFIX}").write_text("")

    with caplog.at_level("WARNING"):
        log.list_chains(DEFAULT_NAMESPACE)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "host" not in messages
    assert str(audit.audit_path("").parent) not in messages


# --------------------------------------------------------------------------
# The store hands out the file log
# --------------------------------------------------------------------------


def test_the_file_store_hands_out_the_file_audit_log() -> None:
    from pipeline.store import FileStore

    assert isinstance(FileStore().audit_log(), FileAuditLog)


# --------------------------------------------------------------------------
# The contract mixins
# --------------------------------------------------------------------------


class TestFileAuditLogContract(
    AuditLogContract,
    SingleNamespaceContract,
    AuditTamperContract,
    AuditConcurrencyContract,
):
    """The shared audit-log contract, run against the file log."""

    def make_log(self) -> FileAuditLog:
        """A log bound to a plan directory of its own, empty until it writes.

        The contract calls ``make_log`` more than once per test and requires
        each log to start empty ("a fresh, empty audit log under test"), so the
        log is bound to its own directory rather than to the plan directory the
        module-level fixture redirects - two views of one directory would share
        state by construction.
        """
        return FileAuditLog(Path(tempfile.mkdtemp()))

    def rewrite_raw(self, log, namespace, chain, entries) -> None:
        """Replace the stored entries of one chain verbatim (test-only hook).

        A malformed entry (``None``) is written as the literal line ``not json``
        so the reader's "unparseable line" path is exercised too.
        """
        path = log.stream_path(chain)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for entry in entries:
            if entry is None:
                lines.append("not json")
            else:
                lines.append(json.dumps(entry, sort_keys=True, separators=(",", ":")))
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
