"""Tests for the external audit-chain anchor (AA-02).

``pipeline.audit`` detects a rewrite, a reorder, or a deletion in the middle of
a chain, but it cannot detect truncation of the tail: a chain whose newest
records were removed still verifies intact, because the stream has no external
record of where it ended. ``PIPELINE_AUDIT_ANCHOR`` supplies that record — an
append-only JSONL log holding the last ``(seq, hash)`` seen for each chain, on
storage the pipeline host cannot rewrite.

These tests pin the opt-out default, one anchor line per changed head, both
tamper verdicts, the fail-closed rejection of a sink that is not a usable
absolute ``file:`` path outside ``PLAN_DIR``, and the guarantee that a broken
sink never stops the daemon's plan sweep.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle; see test_triage_cap_silent_skip
from pipeline import audit as audit_mod
from pipeline import audit_anchor, paths, persistence, watchers

ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both PLAN_DIR bindings at a temp plan directory for one test.

    ``pipeline.audit`` reads ``persistence.PLAN_DIR`` and ``pipeline.watchers``
    reads ``paths.PLAN_DIR`` at call time, so patching the module attributes
    redirects the streams without either module capturing the operator's real
    plan directory at import.
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


@pytest.fixture()
def anchor_file(tmp_path: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable anchoring to an append-only log outside ``PLAN_DIR``.

    Depends on ``plan_dir`` so that fixture's ``PIPELINE_AUDIT_ANCHOR`` cleanup
    runs first regardless of the order the test lists them.
    """
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


def _anchor_lines(path: Path) -> list[dict]:
    """Every anchor record written so far."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _record(plan_name: str = "p") -> dict:
    return audit_mod.append_audit(plan_name, action="dispatch", outcome="ok")


def _truncate(stream: Path, keep: int) -> None:
    """Rewrite *stream* with only its first *keep* lines - the tail cut."""
    lines = stream.read_text().splitlines(keepends=True)
    stream.write_text("".join(lines[:keep]))


def _stream(plan_dir: Path, plan_name: str = "p") -> Path:
    return plan_dir / f"{plan_name}.audit.jsonl"


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, event: dict) -> None:
        self.published.append(event)


def _make_plan_with_marker(plan_dir: Path, plan_name: str) -> None:
    """A plan whose one in-progress story has a finished marker."""
    worktree = plan_dir / f"{plan_name}-wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"exit_code": 0}))
    manifest = {
        "plan": plan_name,
        "stories": {"S-1": {"status": "in_progress", "worktree": str(worktree)}},
    }
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------


def test_unset_anchor_writes_nothing(plan_dir: Path, tmp_path: Path) -> None:
    _record()

    appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert list(tmp_path.glob("**/*.jsonl")) == [_stream(plan_dir)]


def test_empty_anchor_value_writes_nothing(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "")
    _record()

    assert audit_anchor.anchor_heads() == 0


def test_unset_anchor_verifies_every_chain_as_unanchored(plan_dir: Path) -> None:
    _record()

    assert audit_anchor.verify_against_anchor("p") is None


# --------------------------------------------------------------------------
# One anchor line per changed head
# --------------------------------------------------------------------------


def test_first_tick_anchors_the_head(plan_dir: Path, anchor_file: Path) -> None:
    record = _record()

    audit_anchor.anchor_heads()

    lines = _anchor_lines(anchor_file)
    assert [(line["chain"], line["seq"], line["hash"]) for line in lines] == [
        ("p", record["seq"], record["hash"])
    ]


def test_anchor_line_carries_a_utc_timestamp(plan_dir: Path, anchor_file: Path) -> None:
    _record()

    audit_anchor.anchor_heads()

    at = _anchor_lines(anchor_file)[0]["at"]
    assert at.endswith("+00:00")


def test_second_tick_with_no_change_writes_no_second_line(plan_dir: Path, anchor_file: Path) -> None:
    _record()

    audit_anchor.anchor_heads()
    appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert len(_anchor_lines(anchor_file)) == 1


def test_changed_head_is_anchored_again(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    audit_anchor.anchor_heads()
    _record()

    audit_anchor.anchor_heads()

    assert [line["seq"] for line in _anchor_lines(anchor_file)] == [0, 1]


def test_host_chain_is_anchored_too(plan_dir: Path, anchor_file: Path) -> None:
    audit_mod.append_host_audit(action="config_write", outcome="ok")

    audit_anchor.anchor_heads()

    assert [line["chain"] for line in _anchor_lines(anchor_file)] == ["host"]


def test_every_plan_chain_is_anchored(plan_dir: Path, anchor_file: Path) -> None:
    _record("alpha")
    _record("beta")

    audit_anchor.anchor_heads()

    assert sorted(line["chain"] for line in _anchor_lines(anchor_file)) == ["alpha", "beta"]


def test_chain_with_no_records_is_not_anchored(plan_dir: Path, anchor_file: Path) -> None:
    assert audit_anchor.anchor_heads() == 0


def test_anchor_log_is_never_rewritten(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    audit_anchor.anchor_heads()
    first_line = anchor_file.read_text()

    _record()
    audit_anchor.anchor_heads()

    assert anchor_file.read_text().startswith(first_line)


# --------------------------------------------------------------------------
# Tamper verdicts
# --------------------------------------------------------------------------


def test_intact_chain_verifies_against_the_anchor(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    audit_anchor.anchor_heads()

    assert audit_anchor.verify_against_anchor("p") is None


def test_tail_truncation_below_the_anchored_seq_is_detected(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    _record()
    audit_anchor.anchor_heads()
    _record()
    _truncate(_stream(plan_dir), keep=1)

    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 1"


def test_deleting_the_whole_chain_is_detected(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    audit_anchor.anchor_heads()
    _stream(plan_dir).unlink()

    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 0"


def test_editing_the_anchored_record_is_detected(plan_dir: Path, anchor_file: Path) -> None:
    record = _record()
    audit_anchor.anchor_heads()
    # A forger who keeps the chain internally consistent: the record's own hash
    # is recomputed, so only the external anchor can catch this.
    forged = {**record, "outcome": "refused"}
    forged["hash"] = audit_mod._record_hash(forged)
    _stream(plan_dir).write_text(json.dumps(forged) + "\n")

    assert audit_anchor.verify_against_anchor("p") == "anchored hash mismatch at seq 0"


def test_truncation_reported_by_the_verify_audit_tool(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)

    from pipeline import server_tools_lifecycle

    assert server_tools_lifecycle.verify_audit()["anchor"]["p"] == "truncated below anchored seq 1"


# --------------------------------------------------------------------------
# Fail closed on an unusable sink
# --------------------------------------------------------------------------


def test_relative_path_is_refused(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "file:relative/heads.jsonl")

    with pytest.raises(ValueError, match=ANCHOR_VARIABLE):
        audit_anchor.anchor_heads()


def test_unknown_scheme_is_refused(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "s3://bucket/heads.jsonl")

    with pytest.raises(ValueError, match=ANCHOR_VARIABLE):
        audit_anchor.anchor_heads()


def test_path_inside_plan_dir_is_refused(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{plan_dir / 'heads.jsonl'}")

    with pytest.raises(ValueError, match=ANCHOR_VARIABLE):
        audit_anchor.anchor_heads()


def test_refusal_message_never_echoes_the_value(plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "s3://user:hunter2@bucket/heads.jsonl")

    with pytest.raises(ValueError) as excinfo:
        audit_anchor.verify_against_anchor("p")

    assert "hunter2" not in str(excinfo.value)


def test_verify_audit_leaves_anchor_out_when_anchoring_is_off(plan_dir: Path) -> None:
    _record()

    from pipeline import server_tools_lifecycle

    assert "anchor" not in server_tools_lifecycle.verify_audit()


def test_verify_audit_reports_a_deleted_chain_that_was_anchored(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    audit_anchor.anchor_heads()
    _stream(plan_dir).unlink()

    from pipeline import server_tools_lifecycle

    result = server_tools_lifecycle.verify_audit()

    assert result["anchor"] == {"p": "truncated below anchored seq 0"}


# --------------------------------------------------------------------------
# A broken sink never stops the sweep
# --------------------------------------------------------------------------


def test_sink_write_failure_does_not_stop_scan_all_plans(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _make_plan_with_marker(plan_dir, "alpha")

    def _boom() -> int:
        raise OSError("sink unavailable")

    monkeypatch.setattr(audit_anchor, "anchor_heads", _boom)
    monkeypatch.setattr(watchers, "audit_anchor", audit_anchor)
    bus = _FakeBus()

    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        events = watchers.scan_all_plans(bus)

    assert len(events) == 1
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_sink_error_log_names_only_the_error_type(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _make_plan_with_marker(plan_dir, "alpha")

    def _boom() -> int:
        raise OSError("secret-host-path")

    monkeypatch.setattr(audit_anchor, "anchor_heads", _boom)
    monkeypatch.setattr(watchers, "audit_anchor", audit_anchor)

    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        watchers.scan_all_plans(_FakeBus())

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "secret-host-path" not in messages
    assert "OSError" in messages


def test_scan_all_plans_anchors_the_heads(plan_dir: Path, anchor_file: Path) -> None:
    _record()
    _make_plan_with_marker(plan_dir, "alpha")

    watchers.scan_all_plans(_FakeBus())

    assert [line["chain"] for line in _anchor_lines(anchor_file)] == ["p"]


def test_anchor_path_is_read_at_call_time(plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record()
    audit_anchor.anchor_heads()
    later = anchor_file.parent / "later.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{later}")
    _record()

    audit_anchor.anchor_heads()

    assert [line["seq"] for line in _anchor_lines(later)] == [1]


def test_anchor_writes_are_append_only(anchor_file: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record()
    audit_anchor.anchor_heads()
    before = anchor_file.stat().st_ino, anchor_file.read_text()
    _record()

    audit_anchor.anchor_heads()

    after = anchor_file.stat().st_ino, anchor_file.read_text()
    assert after[0] == before[0]
    assert after[1].startswith(before[1])


def test_anchor_uses_append_mode(plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sink is opened with O_APPEND so two writers cannot interleave a line."""
    _record()
    opened: list[int] = []
    real_open = os.open

    def _spy(path, flags, *args, **kwargs):
        if str(path) == str(anchor_file):
            opened.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(audit_anchor.os, "open", _spy)

    audit_anchor.anchor_heads()

    assert opened and all(flags & os.O_APPEND for flags in opened)
