"""An anchor pass enumerates chains through the audit log, not the plan directory.

The pass used to discover plan chains by globbing the plan directory, which
anchored a file store and nothing else. It now asks the audit log for its chains,
so a chain kept anywhere else is anchored the same way, and this module stops
naming the plan directory at all (see ``tests/unit/plan_dir_allowlist.json``).
What must not change with that: the anchor key for each chain, so an existing
anchor log keeps verifying.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle; see test_triage_cap_silent_skip
from pipeline import audit, audit_anchor, decision_records, paths, persistence

ANCHOR_VARIABLE = audit_anchor.PIPELINE_AUDIT_ANCHOR


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit streams at a temp plan directory for one test."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


@pytest.fixture()
def anchor_file(tmp_path: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable anchoring to an append-only log outside the plan directory."""
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


def _anchor_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _stream(plan_dir: Path, plan: str = "p") -> Path:
    return plan_dir / f"{plan}.audit.jsonl"


def _truncate(stream: Path, keep: int) -> None:
    """Rewrite *stream* with only its first *keep* lines - the tail cut."""
    lines = stream.read_text().splitlines(keepends=True)
    stream.write_text("".join(lines[:keep]))


def _decisions_chain(plan: str, records: int = 1) -> Path:
    """Write a decisions chain for *plan*; the tail reader needs seq and hash."""
    path = decision_records.decision_stream_path(plan)
    lines: list[str] = []
    previous = ""
    for index in range(records):
        digest = hashlib.sha256(f"{plan}-decision-{index}".encode()).hexdigest()
        lines.append(
            json.dumps(
                {"seq": index, "prev_hash": previous, "hash": digest},
                sort_keys=True,
            )
        )
        previous = digest
    path.write_text("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# One pass, every chain
# --------------------------------------------------------------------------


def test_one_pass_anchors_a_plan_the_host_and_a_decisions_chain(
    plan_dir: Path, anchor_file: Path
) -> None:
    record = audit.append_audit("alpha", action="dispatch", outcome="ok")
    host = audit.append_host_audit(action="config_write", outcome="ok")
    _decisions_chain("alpha")

    appended = audit_anchor.anchor_heads()

    assert appended == 3
    lines = _anchor_lines(anchor_file)
    assert {line["chain"] for line in lines} == {"alpha", "host", "decisions:alpha"}
    by_chain = {line["chain"]: line for line in lines}
    assert by_chain["alpha"]["seq"] == record["seq"]
    assert by_chain["alpha"]["hash"] == record["hash"]
    # The host chain is anchored under its historical key, "host", not "@host".
    assert by_chain["host"]["seq"] == host["seq"]
    assert by_chain["host"]["hash"] == host["hash"]
    assert by_chain["decisions:alpha"]["seq"] == 0


def test_a_chain_written_through_the_audit_log_is_the_one_anchored(
    plan_dir: Path, anchor_file: Path
) -> None:
    """The head comes from the log, so a moved stream still anchors and verifies."""
    record = audit.append_audit("p", action="dispatch", outcome="ok")

    audit_anchor.anchor_heads()

    assert audit_anchor.verify_against_anchor("p") is None
    assert _anchor_lines(anchor_file) == [
        {**json.loads(anchor_file.read_text()), "chain": "p"}
    ]
    assert _anchor_lines(anchor_file)[0]["hash"] == record["hash"]


def test_an_empty_plan_directory_anchors_nothing(plan_dir: Path, anchor_file: Path) -> None:
    assert audit_anchor.anchor_heads() == 0
    assert _anchor_lines(anchor_file) == []


def test_anchoring_is_off_writes_no_log(tmp_path: Path, plan_dir: Path) -> None:
    audit.append_audit("p", action="dispatch", outcome="ok")

    assert audit_anchor.anchor_heads() == 0
    assert audit_anchor.verify_against_anchor("p") is None
    assert audit_anchor.anchor_report() is None


# --------------------------------------------------------------------------
# A plan name that would alias another chain's anchor key is skipped
# --------------------------------------------------------------------------


def test_a_plan_name_with_a_colon_is_skipped_with_a_warning(
    plan_dir: Path, anchor_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``decisions:other`` would alias the decisions chain of plan ``other``."""
    (plan_dir / "decisions:other.audit.jsonl").write_text(
        json.dumps({"seq": 0, "prev_hash": "", "hash": "ab" * 32}) + "\n"
    )

    with caplog.at_level(logging.WARNING):
        appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert _anchor_lines(anchor_file) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_a_decisions_stream_with_an_unaddressable_name_is_skipped(
    plan_dir: Path, anchor_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (plan_dir / "decisions:other.decisions.chain.jsonl").write_text('{"seq":0,"hash":"ab"}\n')

    with caplog.at_level(logging.WARNING):
        appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert _anchor_lines(anchor_file) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# --------------------------------------------------------------------------
# Tamper detection is unchanged by the new enumeration
# --------------------------------------------------------------------------


def test_a_cut_tail_is_reported_truncated(plan_dir: Path, anchor_file: Path) -> None:
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit_anchor.anchor_heads()

    _truncate(_stream(plan_dir), keep=1)

    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 1"


def test_a_rewritten_anchored_record_is_reported(plan_dir: Path, anchor_file: Path) -> None:
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit_anchor.anchor_heads()
    stream = _stream(plan_dir)
    record = json.loads(stream.read_text().splitlines()[0])
    record["outcome"] = "denied"
    # A forger who keeps the chain internally consistent: the record's own hash
    # is recomputed, so only the external anchor can catch this.
    record["hash"] = audit._record_hash(record)
    stream.write_text(json.dumps(record) + "\n")

    assert audit_anchor.verify_against_anchor("p") == "anchored hash mismatch at seq 0"


def test_a_deleted_chain_is_reported_truncated(plan_dir: Path, anchor_file: Path) -> None:
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit_anchor.anchor_heads()

    _stream(plan_dir).unlink()

    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 0"
    assert audit_anchor.anchor_report() == {"p": "truncated below anchored seq 0"}


def test_a_later_pass_never_lowers_an_anchor(plan_dir: Path, anchor_file: Path) -> None:
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit.append_audit("p", action="dispatch", outcome="ok")
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)

    appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert [line["seq"] for line in _anchor_lines(anchor_file)] == [1]
