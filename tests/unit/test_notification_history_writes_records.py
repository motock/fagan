"""Every notification-history write goes through the shared record store.

The writers in this family (``notification_sinks.file_log_sink``,
``persistence._notify_user``'s direct-write fallback and
``events_nats_consumer._write_dead_letter_record``) must hand their record to
the shared store (``_store.records().append_notification(DEFAULT_NAMESPACE,
plan, record)``) and must not compose a notifications path themselves any more.

The store used here is an in-memory stand-in patched in as
``pipeline.server._store``: it records every append it is given, so a test can
see exactly what a writer handed to the store -- and nothing else.

Written FIRST, before the writers moved onto the store: the routing tests were
RED until the same branch made them green.  The tests that pin what must NOT
change (the JSONL sidecar and the free-text log) were green all along and must
stay green.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

import pytest

import pipeline
from pipeline import events_nats_consumer, notification_sinks, persistence, server
from pipeline.store_records import DEFAULT_NAMESPACE

ALLOWLIST_PATH = Path(__file__).with_name("plan_dir_allowlist.json")

#: The pattern the plan-directory allowlist counts.
PLAN_DIR_PATTERN = re.compile(r"\b(PLAN_DIR|plan_dir)\b")

#: The source directory the modules under test live in.
SOURCE_DIR = Path(pipeline.__file__).parent


class _Records:
    """The record view: what the writers append and what they read back."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.history: dict[tuple[str, str], list[dict]] = {}
        self.raise_on_append: Exception | None = None

    def append_notification(self, namespace, plan_name, record):
        if self.raise_on_append is not None:
            raise self.raise_on_append
        self.calls.append((namespace, plan_name, copy.deepcopy(record)))
        self.history.setdefault((namespace, plan_name), []).append(copy.deepcopy(record))

    def get_notification_history(self, namespace, plan_name):
        return list(self.history.get((namespace, plan_name), [])), 0


class InMemoryRecordStore:
    """A stand-in for ``pipeline.server._store`` that records every append."""

    def __init__(self) -> None:
        self._records = _Records()

    def records(self) -> _Records:
        return self._records

    # The store surface itself, so a writer that reaches for the store
    # directly is recorded just the same.
    def append_notification(self, namespace, plan_name, record):
        return self._records.append_notification(namespace, plan_name, record)

    def get_notification_history(self, namespace, plan_name):
        return self._records.get_notification_history(namespace, plan_name)


class _Bus:
    """A bus that publishes, and delivers (or does not) on demand."""

    def __init__(self, delivers: bool) -> None:
        self._delivers = delivers
        self.published: list[dict] = []

    def publish(self, event) -> None:
        self.published.append(event)

    def delivers(self, event_type) -> bool:
        return self._delivers


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Point the shared store at an in-memory recorder."""
    fake = InMemoryRecordStore()
    monkeypatch.setattr(server, "_store", fake)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return fake


def _event(**payload):
    return {
        "type": "notification",
        "plan": "cap1",
        "ts": "2026-01-02T03:04:05+00:00",
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# file_log_sink -> the store
# ---------------------------------------------------------------------------


def test_file_log_sink_stores_one_record_equal_to_the_record_helper(store):
    """One notification event -> exactly one record in the store, and that
    record is the one persistence._notification_record builds for the event."""
    notification_sinks.file_log_sink(
        _event(
            message="gate failed",
            story_key="S1",
            severity="error",
            event="ci_pending_stalled",
            dedup_key="dk1",
        )
    )

    calls = store.records().calls
    assert len(calls) == 1, "the sink must store exactly one record"
    namespace, plan, record = calls[0]
    assert namespace == DEFAULT_NAMESPACE
    assert plan == "cap1"
    assert record == persistence._notification_record(
        plan_name="cap1",
        message="gate failed",
        story_key="S1",
        severity="error",
        event="ci_pending_stalled",
        dedup_key="dk1",
        ts="2026-01-02T03:04:05+00:00",
    )


def test_file_log_sink_forwards_only_the_context_keys_that_are_present(store):
    """The correlation context that IS in the payload reaches the record; the
    context that is NOT in the payload stays out of it."""
    notification_sinks.file_log_sink(_event(message="hi", correlation_id="corr-1", attempt=2))

    calls = store.records().calls
    assert len(calls) == 1
    record = calls[0][2]
    assert record["correlation_id"] == "corr-1"
    assert record["attempt"] == 2
    for absent in ("role", "provider", "model"):
        assert absent not in record, f"{absent} was not in the payload"


def test_file_log_sink_builds_the_record_with_the_legacy_helper_signature(store, monkeypatch):
    """The helper is called with the legacy signature (no extra keyword
    arguments when the payload carries no context), so a monkeypatched helper
    still produces the record."""
    sentinel = {"__sentinel__": True}

    def fake_record(plan_name, message, story_key, severity, event, dedup_key, ts):
        return sentinel

    monkeypatch.setattr(persistence, "_notification_record", fake_record)
    notification_sinks.file_log_sink(_event(message="x"))

    calls = store.records().calls
    assert len(calls) == 1
    assert calls[0][2] == sentinel


def test_file_log_sink_stores_a_record_without_a_message_for_an_event_without_payload(store):
    """An event with no payload keeps the legacy record: one record whose
    message is None (the helper's default) and whose severity is "info"."""
    notification_sinks.file_log_sink(
        {"type": "notification", "plan": "cap1", "ts": "2026-01-02T03:04:05+00:00"}
    )

    calls = store.records().calls
    assert len(calls) == 1, "the sink must store exactly one record"
    record = calls[0][2]
    assert record["message"] is None
    assert record["severity"] == "info"
    assert record["plan"] == "cap1"


def test_file_log_sink_with_a_falsy_plan_stores_nothing_and_logs_an_error(store, caplog):
    """A falsy plan is refused: nothing is stored and an ERROR is logged."""
    caplog.set_level(logging.DEBUG)
    result = notification_sinks.file_log_sink(
        {"type": "notification", "plan": "", "payload": {"message": "x"}}
    )
    assert result is None
    assert store.records().calls == [], "a falsy plan must store no record"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_file_log_sink_never_raises_when_the_store_fails(store, caplog):
    """A store that raises is logged by error type and the sink still returns
    None -- the never-raise contract survives the move to the store."""
    store.records().raise_on_append = RuntimeError("boom")
    caplog.set_level(logging.DEBUG)

    result = notification_sinks.file_log_sink(_event(message="x"))

    assert result is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a failing store must be logged at ERROR"
    assert "RuntimeError" in caplog.text or any(
        getattr(r, "error_type", None) == "RuntimeError" for r in errors
    ), "the failure must be logged by error type"


# ---------------------------------------------------------------------------
# persistence._notify_user -> the store
# ---------------------------------------------------------------------------


def test_notify_user_stores_one_record_when_the_bus_delivers_nothing(store, monkeypatch):
    """Nothing listens for "notification", so the direct-write fallback runs and
    the record lands in the store exactly once."""
    monkeypatch.setattr("pipeline.event_wiring.get_bus", lambda: _Bus(delivers=False))

    persistence._notify_user(
        "cap1",
        "hello",
        story_key="S1",
        severity="warning",
        event="story_started",
        dedup_key="dk1",
    )

    calls = store.records().calls
    assert len(calls) == 1, "the fallback must store exactly one record"
    namespace, plan, record = calls[0]
    assert namespace == DEFAULT_NAMESPACE
    assert plan == "cap1"
    assert record["message"] == "hello"
    assert record["story_key"] == "S1"
    assert record["severity"] == "warning"
    assert record["event"] == "story_started"
    assert record["dedup_key"] == "dk1"
    assert isinstance(record["ts"], str) and record["ts"]


def test_notify_user_direct_write_does_not_write_the_log_line_itself(store, monkeypatch, tmp_path):
    """The fallback stores the record; it does not compose a notifications path
    and write the free-text line itself any more."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr("pipeline.event_wiring.get_bus", lambda: _Bus(delivers=False))

    persistence._notify_user("cap1", "hello")

    assert len(store.records().calls) == 1
    assert not list(tmp_path.glob("*.notifications.log")), (
        "the direct-write fallback must not write the .log line itself"
    )


def test_notify_user_still_publishes_and_leaves_the_write_to_the_bus(store, monkeypatch):
    """The bus publish path and the "delivers" decision are unchanged: a bus
    that delivers gets the event and no direct write happens."""
    bus = _Bus(delivers=True)
    monkeypatch.setattr("pipeline.event_wiring.get_bus", lambda: bus)

    persistence._notify_user("cap1", "hello")

    assert [e["type"] for e in bus.published] == ["notification"]
    assert store.records().calls == [], (
        "a delivering bus means the sink stores the record, not _notify_user"
    )


# ---------------------------------------------------------------------------
# events_nats_consumer -> the store
# ---------------------------------------------------------------------------


def test_dead_letter_write_stores_one_record_and_repeats_are_idempotent(store):
    """A dead letter is stored once; the same event id is never stored twice."""
    events_nats_consumer._write_dead_letter_record(
        "agent_done", "cap1", {"story_key": "S1"}, "evt-1", 3
    )

    calls = store.records().calls
    assert len(calls) == 1, "a dead letter must store exactly one record"
    namespace, plan, record = calls[0]
    assert namespace == DEFAULT_NAMESPACE
    assert plan == "cap1"
    assert record["event"] == events_nats_consumer.DEAD_LETTER_RECORD_EVENT
    assert record["dedup_key"] == "evt-1"
    assert record["severity"] == "error"
    assert record["story_key"] == "S1"
    assert record["attempt"] == 3
    assert "agent_done" in record["message"]

    events_nats_consumer._write_dead_letter_record(
        "agent_done", "cap1", {"story_key": "S1"}, "evt-1", 3
    )
    assert len(store.records().calls) == 1, "a dead letter must be written once"


def test_record_exists_reads_the_store_not_the_sidecar(store, tmp_path):
    """The existence check asks the store's notification history; the sidecar
    file is neither read nor written any more."""
    store.records().append_notification(
        DEFAULT_NAMESPACE,
        "cap1",
        {
            "ts": "2026-01-02T03:04:05+00:00",
            "plan": "cap1",
            "message": "dead lettered",
            "story_key": None,
            "severity": "error",
            "event": events_nats_consumer.DEAD_LETTER_RECORD_EVENT,
            "dedup_key": "evt-9",
        },
    )
    assert events_nats_consumer._record_exists("cap1", "evt-9"), (
        "the record in the store must be found"
    )

    # A record that only exists in the sidecar file is not seen any more.
    (tmp_path / "cap1.notifications.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-01-02T03:04:05+00:00",
                "plan": "cap1",
                "message": "dead lettered",
                "story_key": None,
                "severity": "error",
                "event": events_nats_consumer.DEAD_LETTER_RECORD_EVENT,
                "dedup_key": "evt-8",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert not events_nats_consumer._record_exists("cap1", "evt-8"), (
        "the sidecar file must not be consulted any more"
    )


# ---------------------------------------------------------------------------
# What must NOT change: the JSONL sidecar and the free-text log
# ---------------------------------------------------------------------------


def test_the_jsonl_sidecar_and_the_free_text_log_keep_their_legacy_shape(tmp_path, monkeypatch):
    """Two notifications still append one JSONL record and one free-text line
    each, in the legacy shapes."""
    from pipeline.store import FileStore

    monkeypatch.setattr(server, "_store", FileStore())
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    notification_sinks.file_log_sink(
        _event(message="first", story_key="S1", severity="error", event="e1", dedup_key="dk1")
    )
    notification_sinks.file_log_sink(_event(message="second"))

    jsonl = (tmp_path / "cap1.notifications.jsonl").read_text().splitlines()
    assert len(jsonl) == 2
    assert json.loads(jsonl[0]) == persistence._notification_record(
        plan_name="cap1",
        message="first",
        story_key="S1",
        severity="error",
        event="e1",
        dedup_key="dk1",
        ts="2026-01-02T03:04:05+00:00",
    )
    log = (tmp_path / "cap1.notifications.log").read_text().splitlines()
    assert log == [
        "2026-01-02T03:04:05+00:00 first",
        "2026-01-02T03:04:05+00:00 second",
    ], "each notification is logged exactly once, in the legacy format"


def test_the_jsonl_sidecar_keeps_a_null_message_for_an_event_without_payload(tmp_path, monkeypatch):
    """An event with no payload still lands in the JSONL sidecar with a null
    message: the store accepts the record the helper builds."""
    from pipeline.store import FileStore

    monkeypatch.setattr(server, "_store", FileStore())
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    notification_sinks.file_log_sink(
        {"type": "notification", "plan": "cap1", "ts": "2026-01-02T03:04:05+00:00"}
    )

    jsonl = (tmp_path / "cap1.notifications.jsonl").read_text().splitlines()
    assert len(jsonl) == 1
    record = json.loads(jsonl[0])
    assert record["message"] is None
    assert record["severity"] == "info"

    # The free-text representation keeps the legacy "<ts> " line: an absent
    # message is empty, never the literal "None" (the old sink built its line
    # from payload.get("message", "")).
    log = (tmp_path / "cap1.notifications.log").read_text().splitlines()
    assert log == ["2026-01-02T03:04:05+00:00 "], (
        "a message-less notification must keep the legacy '<ts> ' log line"
    )


def test_the_store_accepts_a_record_without_a_message(tmp_path, monkeypatch):
    """The store takes the record the helper builds for a notification that
    carries no message: a null message is not a reason to refuse it."""
    from pipeline.store import FileStore

    monkeypatch.setattr(server, "_store", FileStore())
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    server._store.records().append_notification(
        DEFAULT_NAMESPACE,
        "cap1",
        {
            "ts": "2026-01-02T03:04:05+00:00",
            "plan": "cap1",
            "message": None,
            "story_key": None,
            "severity": "info",
            "event": None,
            "dedup_key": None,
        },
    )

    jsonl = (tmp_path / "cap1.notifications.jsonl").read_text().splitlines()
    assert len(jsonl) == 1
    assert json.loads(jsonl[0])["message"] is None


# ---------------------------------------------------------------------------
# The plan-directory allowlist
# ---------------------------------------------------------------------------


def _allowlist() -> dict[str, int]:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def test_the_notification_writers_left_the_plan_directory_allowlist():
    """None of the three modules composes a notifications path any more, so
    none of them may sit on the allowlist."""
    allowlist = _allowlist()
    for gone in (
        "pipeline/notification_sinks.py",
        "pipeline/ticketing.py",
        "pipeline/usage_transitions.py",
    ):
        assert gone not in allowlist, f"{gone} must have left the allowlist"


def test_the_persistence_allowlist_ceiling_was_lowered():
    """persistence.py composes one path less than before, so its ceiling is
    lower than the one it had -- and the ceiling still covers the file."""
    ceiling = _allowlist()["pipeline/persistence.py"]
    assert ceiling < 10, "the persistence ceiling must be lowered"

    source = (SOURCE_DIR / "persistence.py").read_text(encoding="utf-8")
    actual = sum(1 for line in source.splitlines() if PLAN_DIR_PATTERN.search(line))
    assert actual < 10, "persistence.py must compose fewer plan-directory paths"
    assert actual <= ceiling, "the ceiling must still cover the file"


def test_the_notification_writers_no_longer_name_the_plan_directory():
    """The comments that named the plan directory now name the record store."""
    for name in ("notification_sinks.py", "ticketing.py", "usage_transitions.py"):
        source = (SOURCE_DIR / name).read_text(encoding="utf-8")
        assert not PLAN_DIR_PATTERN.search(source), (
            f"{name} must not reference the plan directory any more"
        )


def test_the_rewritten_comments_name_the_record_store():
    """The two comments that used to name the plan directory now name the
    record store, which is where the notification history really lives."""
    for name in ("ticketing.py", "usage_transitions.py"):
        source = (SOURCE_DIR / name).read_text(encoding="utf-8").lower()
        assert "store" in source, f"{name} must name the record store"


def test_the_reference_still_documents_the_notification_sink():
    """The reference keeps documenting the sink that reproduces the legacy log
    behaviour."""
    reference = (SOURCE_DIR.parent / "REFERENCE.md").read_text(encoding="utf-8")
    assert "file_log_sink" in reference


def test_the_dead_letter_docstring_points_at_the_record_store():
    """The dead-letter docstring names the record store, not the old direct
    writer."""
    source = (SOURCE_DIR / "events_nats_consumer.py").read_text(encoding="utf-8")
    assert "direct writer" not in source
    assert "record store" in source


def test_the_dead_letter_module_does_not_grow_by_more_than_five_lines():
    """The dead-letter module stays (nearly) the size it is today."""
    source = (SOURCE_DIR / "events_nats_consumer.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 793
