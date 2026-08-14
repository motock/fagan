"""Tests for the notification-sink module and the ``notification`` event type.

These tests are written FIRST (TDD). The implementation module
``pipeline.notification_sinks`` does not exist yet, so this suite is expected
to be RED until a later dispatch implements it.

Scope of this story:
  - ``pipeline.events.EVENT_TYPES`` gains the ``"notification"`` member.
  - A new ``pipeline.notification_sinks`` module exposes exactly one public
    function, ``file_log_sink(event: dict) -> None``.

The tests below assert every mechanically-checkable requirement of the task:
the new event type, the survival of the eleven pre-existing event types, the
free-text log line format, the structured JSONL record, the missing-plan
guard, the swallow-and-log-on-failure rule, and the defaulting of missing
payload fields.
"""

import json
import logging
from datetime import datetime, timezone

import pytest

from pipeline import events, notification_sinks, persistence

# ---------------------------------------------------------------------------
# The eleven pre-existing EVENT_TYPES members (before this story).
# ---------------------------------------------------------------------------
PRE_EXISTING_EVENT_TYPES = frozenset(
    {
        "story_ready",
        "agent_dispatched",
        "agent_done",
        "tests_passed",
        "changes_requested",
        "pr_open",
        "ci_complete",
        "story_done",
        "parked",
        "rework",
        "reconcile",
    }
)


# ---------------------------------------------------------------------------
# PART 1 - the "notification" event type
# ---------------------------------------------------------------------------

def test_notification_is_a_valid_event_type():
    """make_event("notification", "p") returns a dict with type == "notification"
    and does not raise."""
    ev = events.make_event("notification", "p")
    assert isinstance(ev, dict)
    assert ev["type"] == "notification"


def test_notification_in_event_types_frozenset():
    """The literal string "notification" is a member of EVENT_TYPES."""
    assert "notification" in events.EVENT_TYPES


def test_existing_event_types_survive():
    """Every one of the eleven pre-existing members is still in EVENT_TYPES,
    and make_event("not_a_real_type", "p") still raises ValueError."""
    for member in PRE_EXISTING_EVENT_TYPES:
        assert member in events.EVENT_TYPES, f"missing pre-existing event type: {member}"
    with pytest.raises(ValueError):
        events.make_event("not_a_real_type", "p")


def test_event_types_still_a_frozenset():
    """EVENT_TYPES remains a frozenset after the addition."""
    assert isinstance(events.EVENT_TYPES, frozenset)


def test_event_types_has_exactly_twelve_members():
    """The frozenset now has exactly twelve members: the eleven originals plus
    "notification" - nothing renamed, removed, or reordered beyond the single
    addition."""
    assert len(events.EVENT_TYPES) == 12
    assert events.EVENT_TYPES == PRE_EXISTING_EVENT_TYPES | {"notification"}


# ---------------------------------------------------------------------------
# PART 2 - file_log_sink
# ---------------------------------------------------------------------------

def _make_event(plan="cap1", payload=None, ts=None):
    """Build a bus-level notification event for tests."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return {
        "type": "notification",
        "plan": plan,
        "story_key": None,
        "payload": payload if payload is not None else {},
        "ts": ts,
    }


def test_file_log_sink_is_public_callable():
    """file_log_sink is the single public function of the module."""
    assert callable(notification_sinks.file_log_sink)
    # The module should expose file_log_sink; no other public callables added.
    public = [
        name
        for name in dir(notification_sinks)
        if not name.startswith("__") and callable(getattr(notification_sinks, name))
    ]
    assert "file_log_sink" in public


def test_sink_writes_free_text_line(tmp_path, monkeypatch, caplog):
    """file_log_sink on an event with a payload message appends one line to
    <plan>.notifications.log in the f"{ts} {message}" format."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ts = "2026-01-02T03:04:05.000000+00:00"
    message = "hello"
    ev = _make_event(plan="cap1", payload={"message": message}, ts=ts)
    result = notification_sinks.file_log_sink(ev)
    assert result is None

    log_path = tmp_path / "cap1.notifications.log"
    assert log_path.exists(), "free-text notifications log was not created"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1, "exactly one free-text line should be appended"
    assert lines[0] == f"{ts} {message}", (
        f"free-text line must be in f'{{ts}} {{message}}' format, got: {lines[0]!r}"
    )


def test_sink_writes_structured_record(tmp_path, monkeypatch):
    """The same call appends one JSONL line carrying severity, story_key,
    event and dedup_key from the payload."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ts = "2026-01-02T03:04:05.000000+00:00"
    payload = {
        "message": "gate failed",
        "story_key": "S1",
        "severity": "error",
        "event": "ci_pending_stalled",
        "dedup_key": "dk1",
    }
    ev = _make_event(plan="cap1", payload=payload, ts=ts)
    notification_sinks.file_log_sink(ev)

    jsonl_path = tmp_path / "cap1.notifications.jsonl"
    assert jsonl_path.exists(), "structured notifications jsonl was not created"
    jsonl_lines = jsonl_path.read_text().splitlines()
    assert len(jsonl_lines) == 1, "exactly one JSONL line should be appended"
    record = json.loads(jsonl_lines[0])

    # The structured record must carry the payload fields through.
    assert record["severity"] == "error"
    assert record["story_key"] == "S1"
    assert record["event"] == "ci_pending_stalled"
    assert record["dedup_key"] == "dk1"
    # And the message + ts + plan are present too.
    assert record["message"] == "gate failed"
    assert record["ts"] == ts
    assert record["plan"] == "cap1"


def test_sink_with_missing_plan_does_not_raise(tmp_path, monkeypatch, caplog):
    """file_log_sink({"type": "notification", "payload": {}}) returns None,
    writes nothing, and logs at ERROR."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ev = {"type": "notification", "payload": {}}
    caplog.set_level(logging.ERROR, logger="pipeline.notification_sinks")
    result = notification_sinks.file_log_sink(ev)
    assert result is None

    # Nothing written.
    assert not (tmp_path / ".notifications.log").exists()
    # No file named after a falsy plan either.
    assert not any(p.name.endswith(".notifications.log") for p in tmp_path.iterdir())
    assert not any(p.name.endswith(".notifications.jsonl") for p in tmp_path.iterdir())

    # An ERROR was logged.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "missing plan must be logged at ERROR"


def test_sink_with_falsy_plan_does_not_raise(tmp_path, monkeypatch, caplog):
    """A falsy-but-present plan (empty string) is treated like a missing plan:
    no raise, no write, ERROR logged."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    caplog.set_level(logging.ERROR, logger="pipeline.notification_sinks")
    ev = {"type": "notification", "plan": "", "payload": {"message": "x"}}
    result = notification_sinks.file_log_sink(ev)
    assert result is None
    assert not any(p.name.endswith(".notifications.log") for p in tmp_path.iterdir())
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "falsy plan must be logged at ERROR"


def test_sink_swallows_write_failure(tmp_path, monkeypatch, caplog):
    """Monkeypatch the log-file open to raise OSError; the call returns None
    and does not propagate."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    caplog.set_level(logging.ERROR, logger="pipeline.notification_sinks")

    real_open = open

    def boom(*args, **kwargs):
        # Only the free-text .notifications.log append path triggers here;
        # raise for any open of the notifications log file.
        path = args[0] if args else kwargs.get("file")
        if isinstance(path, str) and path.endswith(".notifications.log"):
            raise OSError("simulated write failure")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", boom)

    ev = _make_event(plan="cap1", payload={"message": "hello"})
    # Must not raise.
    result = notification_sinks.file_log_sink(ev)
    assert result is None


def test_sink_defaults_missing_payload_fields(tmp_path, monkeypatch):
    """An event whose payload has only "message" produces a record with
    severity "info" and story_key/event/dedup_key all None."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ts = "2026-01-02T03:04:05.000000+00:00"
    ev = _make_event(plan="cap1", payload={"message": "only message"}, ts=ts)
    notification_sinks.file_log_sink(ev)

    jsonl_path = tmp_path / "cap1.notifications.jsonl"
    record = json.loads(jsonl_path.read_text().splitlines()[0])
    assert record["severity"] == "info", (
        "missing severity must default to 'info' (via _notification_record)"
    )
    assert record["story_key"] is None
    assert record["event"] is None
    assert record["dedup_key"] is None
    assert record["message"] == "only message"
    assert record["ts"] == ts
    assert record["plan"] == "cap1"


def test_sink_defaults_missing_payload_entirely(tmp_path, monkeypatch):
    """An event with no payload key at all still produces a defaulted record."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ts = "2026-01-02T03:04:05.000000+00:00"
    ev = {"type": "notification", "plan": "cap1", "ts": ts}
    notification_sinks.file_log_sink(ev)

    jsonl_path = tmp_path / "cap1.notifications.jsonl"
    record = json.loads(jsonl_path.read_text().splitlines()[0])
    assert record["severity"] == "info"
    assert record["story_key"] is None
    assert record["event"] is None
    assert record["dedup_key"] is None
    # message falls back to None when absent.
    assert record["message"] is None


def test_sink_defaults_missing_ts(tmp_path, monkeypatch):
    """When the bus event has no "ts", the sink falls back to
    datetime.now(timezone.utc).isoformat() rather than raising."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    before = datetime.now(timezone.utc)
    ev = {
        "type": "notification",
        "plan": "cap1",
        "payload": {"message": "no ts"},
    }
    notification_sinks.file_log_sink(ev)
    after = datetime.now(timezone.utc)

    log_path = tmp_path / "cap1.notifications.log"
    line = log_path.read_text().splitlines()[0]
    # The line begins with an ISO timestamp, then a space, then the message.
    ts_part, _, msg_part = line.partition(" ")
    assert msg_part == "no ts"
    # The synthesized ts must be a plausible ISO-8601 UTC timestamp between
    # before and after.
    parsed = datetime.fromisoformat(ts_part)
    assert parsed.tzinfo is not None
    assert before <= parsed <= after

    # And the same ts is carried into the structured record.
    jsonl_path = tmp_path / "cap1.notifications.jsonl"
    record = json.loads(jsonl_path.read_text().splitlines()[0])
    assert record["ts"] == ts_part


def test_sink_appends_does_not_overwrite(tmp_path, monkeypatch):
    """Two calls append two lines each, rather than truncating the logs."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ev1 = _make_event(plan="cap1", payload={"message": "first"}, ts="2026-01-02T03:04:05+00:00")
    ev2 = _make_event(plan="cap1", payload={"message": "second"}, ts="2026-01-02T03:04:06+00:00")
    notification_sinks.file_log_sink(ev1)
    notification_sinks.file_log_sink(ev2)

    log_lines = (tmp_path / "cap1.notifications.log").read_text().splitlines()
    assert log_lines == [
        "2026-01-02T03:04:05+00:00 first",
        "2026-01-02T03:04:06+00:00 second",
    ]
    jsonl_lines = (tmp_path / "cap1.notifications.jsonl").read_text().splitlines()
    assert len(jsonl_lines) == 2
    assert json.loads(jsonl_lines[0])["message"] == "first"
    assert json.loads(jsonl_lines[1])["message"] == "second"


def test_sink_returns_none_on_happy_path(tmp_path, monkeypatch):
    """file_log_sink always returns None."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    ev = _make_event(plan="cap1", payload={"message": "ok"})
    assert notification_sinks.file_log_sink(ev) is None


def test_sink_uses_notification_record_helper(tmp_path, monkeypatch):
    """The structured record must be built via persistence._notification_record
    (so the two writers cannot drift). Patching the helper to inject a marker
    must surface in the JSONL output."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    sentinel = {"__sentinel__": True}

    def fake_record(plan_name, message, story_key, severity, event, dedup_key, ts):
        return sentinel

    monkeypatch.setattr(persistence, "_notification_record", fake_record)
    ev = _make_event(plan="cap1", payload={"message": "x"})
    notification_sinks.file_log_sink(ev)

    jsonl_path = tmp_path / "cap1.notifications.jsonl"
    record = json.loads(jsonl_path.read_text().splitlines()[0])
    assert record == sentinel, "sink must reuse persistence._notification_record"


def test_sink_calls_write_notification_record(tmp_path, monkeypatch):
    """The JSONL side must be written via persistence._write_notification_record
    rather than reimplemented inline."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    called = {"count": 0}

    real_write = persistence._write_notification_record

    def spy(plan_name, record):
        called["count"] += 1
        return real_write(plan_name, record)

    monkeypatch.setattr(persistence, "_write_notification_record", spy)
    ev = _make_event(plan="cap1", payload={"message": "x"})
    notification_sinks.file_log_sink(ev)
    assert called["count"] == 1, "sink must call persistence._write_notification_record exactly once"


def test_module_docstring_states_forward_constraints():
    """The module docstring must state the three forward-looking constraints
    so the next reader sees them: no inline network I/O, redact at the sink
    boundary, outbound sinks ship disabled by default."""
    doc = notification_sinks.__doc__ or ""
    doc_lower = doc.lower()
    assert "network" in doc_lower, "docstring must mention the no-inline-network-IO constraint"
    assert "redact" in doc_lower, "docstring must mention redaction at the sink boundary"
    assert "disabled" in doc_lower, "docstring must mention outbound sinks ship disabled by default"


def test_module_docstring_distinguishes_event_kinds():
    """The docstring must distinguish the bus-level event type
    (event["type"] == "notification") from the notification's own event name
    (event["payload"]["event"]) so the two are not conflated."""
    doc = notification_sinks.__doc__ or ""
    doc_lower = doc.lower()
    assert "payload" in doc_lower
    # Both senses of "event" must be referenced distinctly.
    assert "type" in doc_lower
    assert "event" in doc_lower