"""Expiry of stale records when a notification outbox is drained (NTF-03).

A channel that cannot deliver keeps every spooled record forever; when it
recovers, weeks of stale notifications are sent at once.  ``_drain_one_outbox``
therefore drops records older than :data:`OUTBOX_MAX_AGE_S` at drain time,
without ever handing them to the sender.

Every record here is written by hand as JSONL (one object per line plus
``\\n``) so a test can state the exact stored bytes.  Timestamps are built from
``datetime.now(UTC)`` minus a timedelta; the exact limit is never asserted
because the clock moves between building a record and draining it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import notification_outbox as ob

REPO_ROOT = Path(__file__).resolve().parents[2]


def _record(message: str, ts: str | None) -> dict:
    """Build one spooled record in the shape ``outbox_sink`` writes."""
    record = {"event": "plan_completed", "plan": "demo", "message": message}
    if ts is not None:
        record["ts"] = ts
    return record


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _write(path: Path, *records: dict) -> list[str]:
    """Write ``records`` as JSONL; return the raw lines (with newlines)."""
    lines = [json.dumps(record) + "\n" for record in records]
    path.write_text("".join(lines), encoding="utf-8")
    return lines


class _Sender:
    """Sender double: records what it received and returns a fixed verdict."""

    def __init__(self, verdict: bool = True) -> None:
        self.verdict = verdict
        self.received: list[dict] = []

    def __call__(self, record: dict) -> bool:
        self.received.append(record)
        return self.verdict


def _stored(path: Path) -> str:
    """Current file text, or ``""`` when the drain removed the file."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_eight_days_old_is_not_sent_and_gone(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    _write(path, _record("stale", _iso(timedelta(days=8))))
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert sender.received == []
    assert _stored(path) == ""


def test_just_under_max_age_is_sent(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    line = _write(
        path, _record("fresh", _iso(timedelta(seconds=ob.OUTBOX_MAX_AGE_S - 3600)))
    )[0]
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert len(sender.received) == 1
    assert sender.received[0]["message"] == "fresh"
    assert line not in _stored(path)


def test_just_over_max_age_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    line = _write(
        path, _record("stale", _iso(timedelta(seconds=ob.OUTBOX_MAX_AGE_S + 3600)))
    )[0]
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert sender.received == []
    assert line not in _stored(path)


def test_missing_ts_is_sent(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    _write(path, _record("no timestamp", None))
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert len(sender.received) == 1
    assert sender.received[0]["message"] == "no timestamp"


def test_unparseable_ts_is_sent(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    _write(path, _record("bad timestamp", "yesterday"))
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert len(sender.received) == 1
    assert sender.received[0]["message"] == "bad timestamp"


def test_naive_ts_is_treated_as_utc_and_dropped(tmp_path: Path) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    naive = (datetime.now(UTC) - timedelta(days=10)).replace(tzinfo=None)
    line = _write(path, _record("naive stale", naive.isoformat()))[0]
    sender = _Sender(True)

    ob._drain_one_outbox(path, sender)

    assert sender.received == []
    assert line not in _stored(path)


def test_failed_send_drops_expired_and_keeps_fresh_byte_for_byte(
    tmp_path: Path,
) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    expired_line, fresh_line = _write(
        path,
        _record("expired", _iso(timedelta(days=8))),
        _record("fresh", _iso(timedelta(hours=1))),
    )
    failing = _Sender(False)

    ob._drain_one_outbox(path, failing)

    assert [record["message"] for record in failing.received] == ["fresh"]
    # The rejected fresh record survives EXACTLY as spooled; the expired one
    # is gone even though nothing was delivered.
    assert _stored(path) == fresh_line
    assert expired_line not in _stored(path)

    accepting = _Sender(True)
    ob._drain_one_outbox(path, accepting)

    assert [record["message"] for record in accepting.received] == ["fresh"]
    assert "fresh" not in _stored(path)


def test_drop_logs_file_name_but_not_record_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "demo.outbox.jsonl"
    _write(path, _record("DO-NOT-LOG-ME", _iso(timedelta(days=8))))
    sender = _Sender(True)

    with caplog.at_level(logging.WARNING):
        ob._drain_one_outbox(path, sender)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(path.name in message for message in messages)
    assert not any("DO-NOT-LOG-ME" in message for message in messages)


def _unreleased_section(changelog: str) -> str:
    """Return the text under ``## [Unreleased]`` up to the next ``## [``."""
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "## [Unreleased]":
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## ["):
            end = index
            break
    return "\n".join(lines[start:end])


def test_changelog_unreleased_mentions_backoff() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "backs off" in _unreleased_section(changelog)
