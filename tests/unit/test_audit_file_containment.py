"""The file audit log refuses a stream whose resolved path leaves its directory.

A chain name is validated before it becomes a file name, but a file that
already sits in the audit directory as a symlink can still point outside it.
Every read and append resolves the stream and refuses one that escapes, so a
write never lands outside the directory the log is bound to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.audit_file import SUFFIX, FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_records import DEFAULT_NAMESPACE


def _log(directory: Path) -> FileAuditLog:
    return FileAuditLog(directory)


def test_appends_and_reads_back_a_plan_chain(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.append(DEFAULT_NAMESPACE, "plan-a", {"event": "one"})
    entries = log.read_chain(DEFAULT_NAMESPACE, "plan-a")

    assert len(entries) == 1
    assert entries[0] is not None
    assert entries[0]["event"] == "one"


def test_appends_and_reads_back_the_host_chain(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, {"event": "host"})
    entries = log.read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)

    assert len(entries) == 1
    assert entries[0] is not None
    assert entries[0]["event"] == "host"


def test_missing_chain_reads_as_none(tmp_path: Path) -> None:
    log = _log(tmp_path)

    assert log.head(DEFAULT_NAMESPACE, "plan-absent") is None
    assert log.read_chain(DEFAULT_NAMESPACE, "plan-absent") == []


def test_refuses_to_append_through_a_symlink_that_leaves_the_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / f"escaped{SUFFIX}"
    target.write_text("")
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / f"plan-b{SUFFIX}").symlink_to(target)
    log = _log(audit_dir)

    with pytest.raises(ValueError, match="audit path escapes the audit directory"):
        log.append(DEFAULT_NAMESPACE, "plan-b", {"event": "leak"})

    assert target.read_text() == ""


def test_refuses_to_read_through_a_symlink_that_leaves_the_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / f"escaped{SUFFIX}"
    target.write_text('{"event": "outside"}\n')
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / f"plan-c{SUFFIX}").symlink_to(target)
    log = _log(audit_dir)

    with pytest.raises(ValueError, match="audit path escapes the audit directory"):
        log.read_chain(DEFAULT_NAMESPACE, "plan-c")
