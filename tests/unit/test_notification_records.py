"""Tests for structured notification records in pipeline/persistence.py.

These tests pin the contract of the new notification-JSONL machinery added to
pipeline/persistence.py:

  * NOTIFY_SEVERITIES module constant
  * _notifications_jsonl_path(plan_name) helper
  * _notification_record(...) pure record builder (with severity fallback)
  * _write_notification_record(...) append helper (OSError-swallowing)
  * _notify_user(...) gains keyword-only story_key/severity/event/dedup_key
    while keeping its two-positional-argument call shape byte-for-byte
    compatible with the ~114 monkeypatch sites across the suite.

The two-positional call `_notify_user(plan_name, message)` must keep behaving
EXACTLY as it does today (same free-text .log line, no new required param), and
the new structured JSONL record is written *after* the free-text line so a
JSONL failure can never cost us the line that exists today.

All tests write into a tmp_path-backed PLAN_DIR via monkeypatch - never the
real ~/.claude/plans (see tests/unit/test_acceptance_journal_test_leak.py).
"""
import json
import logging
import re

import pytest

from pipeline import persistence


# --------------------------------------------------------------------------- #
# Module surface: constant, helpers, __all__, logger
# --------------------------------------------------------------------------- #
def test_notify_severities_constant_exists_and_is_frozenset():
    assert isinstance(persistence.NOTIFY_SEVERITIES, frozenset)
    assert persistence.NOTIFY_SEVERITIES == frozenset({"info", "warning", "error"})


def test_notifications_jsonl_path_shape_mirrors_other_helpers():
    # Must read PLAN_DIR as a free variable (not a captured default arg), so
    # patching persistence.PLAN_DIR at runtime is reflected.
    assert persistence._notifications_jsonl_path("p") == persistence.PLAN_DIR / "p.notifications.jsonl"


def test_notifications_jsonl_path_respects_patched_plan_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    assert persistence._notifications_jsonl_path("p") == tmp_path / "p.notifications.jsonl"


def test_notification_record_keys_and_insertion_order():
    record = persistence._notification_record(
        "p", "m", story_key="S1", severity="warning",
        event="ci_pending_stalled", dedup_key="ci_pending_stalled:S1", ts="2026-01-01T00:00:00+00:00",
    )
    assert list(record.keys()) == [
        "ts", "plan", "message", "story_key", "severity", "event", "dedup_key",
    ]
    assert record == {
        "ts": "2026-01-01T00:00:00+00:00",
        "plan": "p",
        "message": "m",
        "story_key": "S1",
        "severity": "warning",
        "event": "ci_pending_stalled",
        "dedup_key": "ci_pending_stalled:S1",
    }


def test_notification_record_bad_severity_falls_back_to_info(caplog):
    with caplog.at_level(logging.WARNING, logger="pipeline.persistence"):
        record = persistence._notification_record(
            "p", "m", story_key=None, severity="catastrophic",
            event=None, dedup_key=None, ts="2026-01-01T00:00:00+00:00",
        )
    assert record["severity"] == "info"
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_notification_record_none_severity_falls_back_to_info(caplog):
    with caplog.at_level(logging.WARNING, logger="pipeline.persistence"):
        record = persistence._notification_record(
            "p", "m", story_key=None, severity=None,
            event=None, dedup_key=None, ts="2026-01-01T00:00:00+00:00",
        )
    assert record["severity"] == "info"


def test_notification_record_non_string_severity_falls_back_to_info(caplog):
    with caplog.at_level(logging.WARNING, logger="pipeline.persistence"):
        record = persistence._notification_record(
            "p", "m", story_key=None, severity=123,
            event=None, dedup_key=None, ts="2026-01-01T00:00:00+00:00",
        )
    assert record["severity"] == "info"


def test_notification_record_never_raises_on_bad_severity():
    # Called from failure paths; must never break a tick.
    for bad in (None, 123, object(), "", "catastrophic", ["info"]):
        persistence._notification_record(
            "p", "m", story_key=None, severity=bad,
            event=None, dedup_key=None, ts="2026-01-01T00:00:00+00:00",
        )


def test_write_notification_record_appends_jsonl_line(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    record = {"ts": "t", "plan": "p", "message": "m", "story_key": None,
              "severity": "info", "event": None, "dedup_key": None}
    persistence._write_notification_record("p", record)
    path = tmp_path / "p.notifications.jsonl"
    assert path.exists()
    line = path.read_text(encoding="utf-8")
    assert line.endswith("\n")
    assert json.loads(line) == record


def test_write_notification_record_two_appends_two_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._write_notification_record("p", {"ts": "t1"})
    persistence._write_notification_record("p", {"ts": "t2"})
    path = tmp_path / "p.notifications.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"ts": "t1"}
    assert json.loads(lines[1]) == {"ts": "t2"}


def test_write_notification_record_swallows_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    def boom(plan_name):
        raise OSError("disk full")

    monkeypatch.setattr(persistence, "_notifications_jsonl_path", boom)
    # Must not raise.
    persistence._write_notification_record("p", {"ts": "t"})


def test_module_has_logger():
    assert isinstance(persistence.logger, logging.Logger)
    assert persistence.logger.name == "pipeline.persistence"


def test_all_contains_new_symbols_and_stays_sorted():
    names = persistence.__all__
    for sym in ("NOTIFY_SEVERITIES", "_notification_record",
                "_notifications_jsonl_path", "_write_notification_record"):
        assert sym in names, f"{sym} missing from __all__"
    assert names == sorted(names), "__all__ must stay alphabetically sorted"


def test_logging_import_present():
    # The module must `import logging` (module had neither logging nor a
    # logger today).
    import inspect
    src = inspect.getsource(persistence)
    assert re.search(r"^import logging\b", src, re.MULTILINE), (
        "pipeline/persistence.py must add `import logging`"
    )


# --------------------------------------------------------------------------- #
# _notify_user behavior
# --------------------------------------------------------------------------- #
def test_positional_call_writes_unchanged_log_line(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._notify_user("p", "hello")
    log_path = tmp_path / "p.notifications.log"
    assert log_path.exists()
    content = log_path.read_text()
    lines = content.splitlines()
    assert len(lines) == 1
    assert re.match(r"^\S+ hello$", lines[0]), (
        f"log line must be '<ISO ts> hello', got {lines[0]!r}"
    )


def test_positional_call_writes_default_jsonl_record(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._notify_user("p", "hello")
    jsonl_path = tmp_path / "p.notifications.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["severity"] == "info"
    assert record["story_key"] is None
    assert record["event"] is None
    assert record["dedup_key"] is None
    assert record["message"] == "hello"
    assert record["plan"] == "p"


def test_keyword_fields_are_recorded_verbatim(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._notify_user(
        "p", "m", story_key="S1", severity="warning",
        event="ci_pending_stalled", dedup_key="ci_pending_stalled:S1",
    )
    record = json.loads(
        (tmp_path / "p.notifications.jsonl").read_text(encoding="utf-8")
    )
    assert record["story_key"] == "S1"
    assert record["severity"] == "warning"
    assert record["event"] == "ci_pending_stalled"
    assert record["dedup_key"] == "ci_pending_stalled:S1"


def test_third_positional_argument_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    with pytest.raises(TypeError):
        persistence._notify_user("p", "m", "S1")


def test_unknown_severity_falls_back_to_info(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="pipeline.persistence"):
        persistence._notify_user("p", "m", severity="catastrophic")
    record = json.loads(
        (tmp_path / "p.notifications.jsonl").read_text(encoding="utf-8")
    )
    assert record["severity"] == "info"
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_jsonl_write_failure_does_not_propagate(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    def boom(plan_name):
        raise OSError("disk full")

    monkeypatch.setattr(persistence, "_notifications_jsonl_path", boom)
    result = persistence._notify_user("p", "m")
    assert result is None
    # The free-text .log line must still be present.
    assert (tmp_path / "p.notifications.log").exists()
    assert (tmp_path / "p.notifications.log").read_text().splitlines() == ["m"] or \
        (tmp_path / "p.notifications.log").read_text().strip().endswith(" m")


def test_jsonl_write_failure_still_writes_log_line(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)

    def boom(plan_name):
        raise OSError("disk full")

    monkeypatch.setattr(persistence, "_notifications_jsonl_path", boom)
    persistence._notify_user("p", "hello")
    log_line = (tmp_path / "p.notifications.log").read_text().splitlines()[0]
    assert re.match(r"^\S+ hello$", log_line)


def test_two_calls_produce_two_valid_jsonl_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._notify_user("p", "first")
    persistence._notify_user("p", "second")
    path = tmp_path / "p.notifications.jsonl"
    raw = path.read_text(encoding="utf-8")
    # newline-terminated, no trailing blank line
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")
    lines = raw.splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
    assert json.loads(lines[0])["message"] == "first"
    assert json.loads(lines[1])["message"] == "second"


def test_timestamp_is_shared_between_log_and_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    persistence._notify_user("p", "hello")
    log_line = (tmp_path / "p.notifications.log").read_text().splitlines()[0]
    record = json.loads(
        (tmp_path / "p.notifications.jsonl").read_text(encoding="utf-8")
    )
    # The ISO prefix of the .log line equals the record's "ts".
    log_ts = log_line.split(" ", 1)[0]
    assert log_ts == record["ts"]


def test_notify_user_signature_is_keyword_only_for_new_params():
    import inspect
    sig = inspect.signature(persistence._notify_user)
    params = list(sig.parameters.values())
    # plan_name, message positional-or-keyword; then keyword-only rest.
    assert [p.name for p in params[:2]] == ["plan_name", "message"]
    kw_only = {p.name for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY}
    assert kw_only == {
        "story_key", "severity", "event", "dedup_key",
        "correlation_id", "attempt", "role", "provider", "model",
    }
    # Defaults: severity="info", the rest None.
    assert params[2].default is None  # story_key
    assert params[3].default == "info"  # severity
    assert params[4].default is None  # event
    assert params[5].default is None  # dedup_key
    # The correlation/context params added by W4L-01: keyword-only, None.
    for name in ("correlation_id", "attempt", "role", "provider", "model"):
        p = sig.parameters[name]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be keyword-only"
        )
        assert p.default is None, f"{name} must default to None"