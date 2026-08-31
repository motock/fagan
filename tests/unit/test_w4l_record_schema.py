"""W4L structured-record schema extension: correlation_id + dispatch context.

Story under test: make the structured notification/event schema able to carry
a ``correlation_id`` (plus ``attempt``/``role``/``provider``/``model``
context) so later stories can stamp dispatch → review → rework → merge
records with it.  This story is fields plumbing ONLY — no new emitters and no
correlation-ID minting.

Surfaces pinned here:

  * ``pipeline.persistence._notification_record`` gains optional
    ``correlation_id`` / ``attempt`` / ``role`` / ``provider`` / ``model``
    keyword parameters defaulting to ``None``; each is included in the
    returned record dict ONLY when not None.  With none of them set the
    record shape is byte-for-byte what it was before (existing tests pin the
    exact dict).
  * ``pipeline.persistence._notify_user`` accepts and forwards the same
    optional kwargs (default ``None``) so its callers keep working unchanged.
  * ``pipeline.notification_sinks.file_log_sink`` reads
    ``payload.get("correlation_id")`` (and the same optional context keys)
    off the bus event's payload and passes them through to
    ``_notification_record``.
  * ``pipeline.events.make_event`` gains an optional ``correlation_id``
    kwarg; when set it appears as a TOP-LEVEL ``"correlation_id"`` key on the
    event dict, and when ``None``/omitted the key is absent entirely (so the
    legacy 5-key event shape is unchanged).

All tests write into a tmp_path-backed PLAN_DIR via monkeypatch - never the
real ~/.claude/plans (see tests/unit/test_acceptance_journal_test_leak.py).
"""
import inspect
import json

from pipeline import events, persistence
from pipeline.notification_sinks import file_log_sink

# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
NEW_RECORD_KWARGS = ("correlation_id", "attempt", "role", "provider", "model")

BASE_RECORD_KWARGS = {
    "story_key": "S1",
    "severity": "warning",
    "event": "ci_pending_stalled",
    "dedup_key": "ci_pending_stalled:S1",
    "ts": "2026-01-01T00:00:00+00:00",
}

# The exact legacy record shape, pinned by tests/unit/test_notification_records.py.
LEGACY_RECORD = {
    "ts": "2026-01-01T00:00:00+00:00",
    "plan": "p",
    "message": "m",
    "story_key": "S1",
    "severity": "warning",
    "event": "ci_pending_stalled",
    "dedup_key": "ci_pending_stalled:S1",
}

LEGACY_EVENT_KEYS = {"type", "plan", "story_key", "payload", "ts"}


class _NoSinkBus:
    """Bus stub with no ``_handlers`` so _notify_user takes its direct-write path."""

    def publish(self, event):
        pass

    def subscribe(self, *args, **kwargs):
        pass


def _capture_written_records(monkeypatch):
    """Replace persistence._write_notification_record with a capturer."""
    captured = []

    def _fake_write(plan_name, record):
        captured.append((plan_name, dict(record)))

    monkeypatch.setattr(persistence, "_write_notification_record", _fake_write)
    return captured


# --------------------------------------------------------------------------- #
# 1. _notification_record backward compatibility (no new args)
# --------------------------------------------------------------------------- #
def test_notification_record_without_new_fields_matches_legacy_shape_exactly():
    record = persistence._notification_record("p", "m", **BASE_RECORD_KWARGS)
    assert record == LEGACY_RECORD
    assert list(record.keys()) == [
        "ts", "plan", "message", "story_key", "severity", "event", "dedup_key",
    ]


def test_notification_record_new_params_exist_and_default_to_none():
    sig = inspect.signature(persistence._notification_record)
    for name in NEW_RECORD_KWARGS:
        assert name in sig.parameters, f"missing new parameter: {name}"
        assert sig.parameters[name].default is None, f"{name} must default to None"


# --------------------------------------------------------------------------- #
# 2. All five new fields land in the record with their values
# --------------------------------------------------------------------------- #
def test_notification_record_with_all_five_new_fields():
    record = persistence._notification_record(
        "p", "m",
        correlation_id="abc", attempt=2, role="dispatch",
        provider="ollama", model="glm-5.3-flash:cloud",
        **BASE_RECORD_KWARGS,
    )
    assert record["correlation_id"] == "abc"
    assert record["attempt"] == 2
    assert record["role"] == "dispatch"
    assert record["provider"] == "ollama"
    assert record["model"] == "glm-5.3-flash:cloud"
    # Legacy fields are untouched alongside the new ones.
    for key, value in LEGACY_RECORD.items():
        assert record[key] == value, f"legacy field {key!r} changed"
    assert set(record) == set(LEGACY_RECORD) | set(NEW_RECORD_KWARGS)


def test_notification_record_severity_fallback_still_works_with_new_fields():
    record = persistence._notification_record(
        "p", "m", severity="catastrophic", story_key=None, event=None,
        dedup_key=None, ts="t", correlation_id="abc", attempt=1,
    )
    assert record["severity"] == "info"
    assert record["correlation_id"] == "abc"
    assert record["attempt"] == 1


# --------------------------------------------------------------------------- #
# 3. Negative: None / omitted → key absent, never present-as-null
# --------------------------------------------------------------------------- #
def test_notification_record_explicit_correlation_id_none_leaves_key_absent():
    record = persistence._notification_record(
        "p", "m", correlation_id=None, **BASE_RECORD_KWARGS
    )
    assert "correlation_id" not in record
    assert record == LEGACY_RECORD


def test_notification_record_omitted_new_fields_leave_all_five_keys_absent():
    record = persistence._notification_record("p", "m", **BASE_RECORD_KWARGS)
    for name in NEW_RECORD_KWARGS:
        assert name not in record, f"{name!r} must be absent when omitted, not None"


def test_notification_record_mixed_set_and_none_only_set_keys_present():
    record = persistence._notification_record(
        "p", "m",
        correlation_id="abc", attempt=None, role="review",
        provider=None, model=None,
        story_key=None, severity="info", event=None, dedup_key=None, ts="t",
    )
    assert record["correlation_id"] == "abc"
    assert record["role"] == "review"
    assert "attempt" not in record
    assert "provider" not in record
    assert "model" not in record


def test_notification_record_falsy_but_not_none_values_are_kept():
    # The rule is "include ONLY when not None" - falsy values are still set.
    record = persistence._notification_record(
        "p", "m",
        correlation_id="", attempt=0, role="", provider="", model="",
        story_key=None, severity="info", event=None, dedup_key=None, ts="t",
    )
    assert record["correlation_id"] == ""
    assert record["attempt"] == 0
    assert record["role"] == ""
    assert record["provider"] == ""
    assert record["model"] == ""


# --------------------------------------------------------------------------- #
# _notify_user forwards the same optional kwargs
# --------------------------------------------------------------------------- #
def test_notify_user_forwards_new_context_fields_into_record(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    from pipeline import event_wiring

    monkeypatch.setattr(event_wiring, "get_bus", lambda: _NoSinkBus())
    captured = _capture_written_records(monkeypatch)

    persistence._notify_user(
        "p", "m",
        correlation_id="abc", attempt=2, role="dispatch",
        provider="ollama", model="glm-5.3-flash:cloud",
    )

    assert len(captured) == 1
    plan, record = captured[0]
    assert plan == "p"
    assert record["correlation_id"] == "abc"
    assert record["attempt"] == 2
    assert record["role"] == "dispatch"
    assert record["provider"] == "ollama"
    assert record["model"] == "glm-5.3-flash:cloud"


def test_notify_user_without_new_kwargs_record_has_no_new_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    from pipeline import event_wiring

    monkeypatch.setattr(event_wiring, "get_bus", lambda: _NoSinkBus())
    captured = _capture_written_records(monkeypatch)

    persistence._notify_user("p", "m")

    assert len(captured) == 1
    _, record = captured[0]
    assert set(record) == set(LEGACY_RECORD)
    for name in NEW_RECORD_KWARGS:
        assert name not in record


# --------------------------------------------------------------------------- #
# 4. make_event correlation_id → top-level key
# --------------------------------------------------------------------------- #
def test_make_event_with_correlation_id_has_top_level_key():
    evt = events.make_event("agent_dispatched", "p1", correlation_id="abc")
    assert evt["correlation_id"] == "abc"
    assert set(evt) == LEGACY_EVENT_KEYS | {"correlation_id"}


def test_make_event_without_correlation_id_omits_key():
    evt = events.make_event("agent_dispatched", "p1")
    assert "correlation_id" not in evt
    assert set(evt) == LEGACY_EVENT_KEYS


def test_make_event_explicit_correlation_id_none_omits_key():
    evt = events.make_event("agent_dispatched", "p1", correlation_id=None)
    assert "correlation_id" not in evt
    assert set(evt) == LEGACY_EVENT_KEYS


def test_make_event_correlation_id_coexists_with_story_key_and_payload():
    payload = {"message": "hi"}
    evt = events.make_event(
        "notification", "p1", story_key="S1", payload=payload,
        correlation_id="abc",
    )
    assert evt["correlation_id"] == "abc"
    assert evt["story_key"] == "S1"
    # correlation_id is top-level; the caller's payload is not mutated.
    assert evt["payload"] == {"message": "hi"}


# --------------------------------------------------------------------------- #
# 5. file_log_sink forwards payload correlation context into the JSONL record
# --------------------------------------------------------------------------- #
def test_file_log_sink_forwards_correlation_context_from_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    captured = _capture_written_records(monkeypatch)

    event = {
        "type": "notification",
        "plan": "p1",
        "story_key": "S1",
        "payload": {
            "message": "m",
            "story_key": "S1",
            "severity": "info",
            "correlation_id": "abc",
            "attempt": 2,
            "role": "dispatch",
            "provider": "ollama",
            "model": "glm-5.3-flash:cloud",
        },
        "ts": "2026-01-01T00:00:00+00:00",
    }
    file_log_sink(event)

    assert len(captured) == 1, "sink must write exactly one JSONL record"
    plan, record = captured[0]
    assert plan == "p1"
    assert record["correlation_id"] == "abc"
    assert record["attempt"] == 2
    assert record["role"] == "dispatch"
    assert record["provider"] == "ollama"
    assert record["model"] == "glm-5.3-flash:cloud"


def test_file_log_sink_forwards_only_the_context_keys_present_in_payload(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    captured = _capture_written_records(monkeypatch)

    event = {
        "type": "notification",
        "plan": "p1",
        "payload": {"message": "m", "correlation_id": "abc"},
        "ts": "2026-01-01T00:00:00+00:00",
    }
    file_log_sink(event)

    _, record = captured[0]
    assert record["correlation_id"] == "abc"
    for name in ("attempt", "role", "provider", "model"):
        assert name not in record


def test_file_log_sink_writes_correlation_id_into_real_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    event = {
        "type": "notification",
        "plan": "p1",
        "payload": {"message": "m", "correlation_id": "abc"},
        "ts": "2026-01-01T00:00:00+00:00",
    }
    file_log_sink(event)

    lines = (tmp_path / "p1.notifications.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["correlation_id"] == "abc"


# --------------------------------------------------------------------------- #
# 6. Negative: sink without correlation context still writes a valid record
# --------------------------------------------------------------------------- #
def test_file_log_sink_payload_without_correlation_id_writes_valid_record(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    captured = _capture_written_records(monkeypatch)

    event = {
        "type": "notification",
        "plan": "p1",
        "payload": {"message": "m", "story_key": "S1", "severity": "info"},
        "ts": "2026-01-01T00:00:00+00:00",
    }
    file_log_sink(event)  # must not raise KeyError

    assert len(captured) == 1
    _, record = captured[0]
    assert record["plan"] == "p1"
    assert record["message"] == "m"
    assert set(record) == set(LEGACY_RECORD)
    assert "correlation_id" not in record


def test_file_log_sink_payload_correlation_id_none_leaves_key_absent(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    captured = _capture_written_records(monkeypatch)

    event = {
        "type": "notification",
        "plan": "p1",
        "payload": {"message": "m", "correlation_id": None},
        "ts": "2026-01-01T00:00:00+00:00",
    }
    file_log_sink(event)

    _, record = captured[0]
    assert "correlation_id" not in record


def test_file_log_sink_event_without_payload_key_still_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    captured = _capture_written_records(monkeypatch)

    event = {"type": "notification", "plan": "p1", "ts": "2026-01-01T00:00:00+00:00"}
    file_log_sink(event)  # must not raise

    assert len(captured) == 1
    _, record = captured[0]
    assert record["plan"] == "p1"
    assert "correlation_id" not in record