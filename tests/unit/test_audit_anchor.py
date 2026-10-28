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
from collections.abc import Iterator
from contextlib import contextmanager
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
# The strongest anchor is never lowered
# --------------------------------------------------------------------------


def test_a_later_tick_does_not_reanchor_a_truncated_head(plan_dir: Path, anchor_file: Path) -> None:
    """Cutting the tail and waiting one daemon tick must not launder the chain."""
    _record()
    _record()
    audit_anchor.anchor_heads()
    before = anchor_file.read_text()
    _truncate(_stream(plan_dir), keep=1)

    appended = audit_anchor.anchor_heads()  # the next 60s tick

    assert appended == 0
    assert anchor_file.read_text() == before
    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 1"


def test_truncation_is_still_reported_by_verify_audit_after_a_later_tick(
    plan_dir: Path, anchor_file: Path
) -> None:
    _record()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)
    audit_anchor.anchor_heads()

    from pipeline import server_tools_lifecycle

    assert server_tools_lifecycle.verify_audit()["anchor"]["p"] == "truncated below anchored seq 1"


def test_a_later_tick_does_not_reanchor_an_edited_anchored_record(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A rewritten record at the anchored seq stays anchored to the original hash."""
    record = _record()
    audit_anchor.anchor_heads()
    forged = {**record, "outcome": "refused"}
    forged["hash"] = audit_mod._record_hash(forged)
    _stream(plan_dir).write_text(json.dumps(forged) + "\n")
    before = anchor_file.read_text()

    appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert anchor_file.read_text() == before
    assert audit_anchor.verify_against_anchor("p") == "anchored hash mismatch at seq 0"


def test_edited_record_is_still_reported_by_verify_audit_after_a_later_tick(
    plan_dir: Path, anchor_file: Path
) -> None:
    record = _record()
    audit_anchor.anchor_heads()
    forged = {**record, "outcome": "refused"}
    forged["hash"] = audit_mod._record_hash(forged)
    _stream(plan_dir).write_text(json.dumps(forged) + "\n")
    audit_anchor.anchor_heads()

    from pipeline import server_tools_lifecycle

    assert server_tools_lifecycle.verify_audit()["anchor"]["p"] == "anchored hash mismatch at seq 0"


def test_refusing_to_lower_an_anchor_logs_chain_and_seqs_only(
    plan_dir: Path, anchor_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _record()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)

    with caplog.at_level(logging.WARNING, logger="pipeline.audit_anchor"):
        audit_anchor.anchor_heads()

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "p" in messages
    assert "1" in messages
    assert "0" in messages
    assert str(anchor_file) not in messages
    assert str(plan_dir) not in messages


def test_a_malformed_anchor_line_is_ignored(plan_dir: Path, anchor_file: Path) -> None:
    """A hand-mangled anchor line is not read as an anchor at all."""
    _record()
    anchor_file.write_text(
        json.dumps({"chain": "p", "seq": None, "hash": ""}) + "\n"
        + json.dumps({"chain": "p", "seq": "7", "hash": "abc"}) + "\n"
    )

    assert audit_anchor.verify_against_anchor("p") is None


def test_unreadable_stream_is_reported_as_tampered_not_raised(plan_dir: Path, anchor_file: Path) -> None:
    _record("q")
    audit_anchor.anchor_heads()
    stream = _stream(plan_dir, "q")
    stream.unlink()
    stream.mkdir()

    assert audit_anchor.verify_against_anchor("q") == "truncated below anchored seq 0"


def test_unreadable_stream_does_not_abort_the_anchor_pass(plan_dir: Path, anchor_file: Path) -> None:
    _record("p")
    _record("q")
    audit_anchor.anchor_heads()
    stream = _stream(plan_dir, "q")
    stream.unlink()
    stream.mkdir()
    _record("p")

    appended = audit_anchor.anchor_heads()

    assert appended == 1
    assert [line["chain"] for line in _anchor_lines(anchor_file)].count("p") == 2
    assert [line["chain"] for line in _anchor_lines(anchor_file)].count("p") == 2


# --------------------------------------------------------------------------
# An unreadable anchor log fails closed
# --------------------------------------------------------------------------


@contextmanager
def _refuse_reads(monkeypatch: pytest.MonkeyPatch, path: Path) -> Iterator[None]:
    """Make opening *path* raise for the duration of the ``with`` block.

    A permission change cannot be simulated with ``chmod`` on an append-only
    mount, and root ignores the mode bits, so the refusal is injected instead;
    the file's bytes stay exactly as they were.
    """
    real_open = Path.open

    def _refuse(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise PermissionError(13, "permission denied")
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", _refuse)
        yield


def test_unreadable_anchor_log_reports_tampered_not_clean(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log that cannot be read must not read as "nothing was anchored"."""
    _record()
    audit_anchor.anchor_heads()

    with _refuse_reads(monkeypatch, anchor_file):
        verdict = audit_anchor.verify_against_anchor("p")
        report = audit_anchor.anchor_report()

    assert verdict == "anchor log unreadable: PermissionError"
    assert report
    assert all(value is not None for value in report.values())


def test_unreadable_anchor_log_keeps_the_previous_tamper_verdict(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)
    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 1"

    with _refuse_reads(monkeypatch, anchor_file):
        verdict = audit_anchor.verify_against_anchor("p")

    assert verdict == "anchor log unreadable: PermissionError"


def test_unreadable_anchor_log_appends_nothing(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that cannot read the anchors must not re-anchor the current head.

    Re-anchoring here would record a head below the cut and launder the
    truncation, so the pass is skipped and the log stays byte-identical.
    """
    _record()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)
    before = anchor_file.read_bytes()

    with _refuse_reads(monkeypatch, anchor_file):
        appended = audit_anchor.anchor_heads()

    assert appended == 0
    assert anchor_file.read_bytes() == before
    assert audit_anchor.verify_against_anchor("p") == "truncated below anchored seq 1"


def test_a_directory_in_place_of_the_anchor_log_is_reported(
    plan_dir: Path, anchor_file: Path
) -> None:
    """The real-filesystem shape of the same failure: a path that cannot be read."""
    _record()
    audit_anchor.anchor_heads()
    anchor_file.unlink()
    anchor_file.mkdir()

    verdict = audit_anchor.verify_against_anchor("p")

    assert verdict is not None
    assert verdict.startswith("anchor log unreadable:")
    assert audit_anchor.anchor_heads() == 0


def test_unreadable_anchor_log_never_stops_the_scan(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _record()
    _make_plan_with_marker(plan_dir, "alpha")

    with (
        caplog.at_level(logging.WARNING, logger="pipeline.audit_anchor"),
        _refuse_reads(monkeypatch, anchor_file),
    ):
        events = watchers.scan_all_plans(_FakeBus())

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert events is not None
    assert "PermissionError" in messages
    assert str(anchor_file) not in messages
    assert str(plan_dir) not in messages


def test_anchor_record_with_a_traversing_chain_name_is_not_followed(
    plan_dir: Path, anchor_file: Path
) -> None:
    """A hand-written chain name cannot steer the read outside ``PLAN_DIR``."""
    outside = plan_dir.parent / "outside.jsonl"
    outside.write_text(json.dumps({"seq": 0, "hash": "abc"}) + "\n")
    anchor_file.write_text(json.dumps({"chain": "../outside", "seq": 0, "hash": "abc"}) + "\n")

    assert audit_anchor.verify_against_anchor("../outside") is None
    assert audit_anchor.anchor_report() == {}


def test_verify_audit_reports_an_unreadable_anchor_log_as_tampered(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool's answer must never look clean just because the sink broke."""
    from pipeline import server_tools_lifecycle

    _record()
    audit_anchor.anchor_heads()

    with _refuse_reads(monkeypatch, anchor_file):
        anchor = server_tools_lifecycle.verify_audit()["anchor"]

    assert anchor
    assert all(value is not None for value in anchor.values())


def test_a_read_that_fails_mid_log_is_not_a_partial_anchor_set(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-read anchor set must not be used as if it were the whole one."""
    _record()
    _record()
    audit_anchor.anchor_heads()
    _record()
    audit_anchor.anchor_heads()
    _truncate(_stream(plan_dir), keep=1)
    before = anchor_file.read_bytes()
    real_parsed = audit_anchor._parsed

    def _breaks_after_first(handle):
        for index, record in enumerate(real_parsed(handle)):
            if index == 1:
                raise OSError(5, "input/output error")
            yield record

    with monkeypatch.context() as patch:
        patch.setattr(audit_anchor, "_parsed", _breaks_after_first)
        appended = audit_anchor.anchor_heads()
        verdict = audit_anchor.verify_against_anchor("p")

    assert appended == 0
    assert anchor_file.read_bytes() == before
    assert verdict == "anchor log unreadable: OSError"
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
