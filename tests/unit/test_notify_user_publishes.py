"""Tests for the bus-publishing refactor of ``_notify_user`` in
``pipeline/persistence.py``.

This story replaces the BODY of ``_notify_user`` so that it:

  1. Computes ``ts`` once (as today).
  2. Builds the structured record via the existing ``_notification_record`` helper
     (so the severity fallback stays single-sourced).
  3. Builds a bus event with ``make_event("notification", plan_name, ...)``
     and publishes it via a LAZY ``from .event_wiring import get_bus`` import
     inside the function, then ``get_bus().publish(evt)``.
  4. On ANY exception from the lazy import, ``get_bus()``, or ``publish()``,
     falls back to writing the two files directly (the exact code the previous
     story had in the body) and logs at ERROR. A broken bus must never silence
     a notification.

The signature of ``_notify_user`` stays exactly as the previous story left it
(keyword-only ``story_key``/``severity``/``event``/``dedup_key`` with the same
defaults), and ``_write_notification_record`` / ``_notification_record`` /
``_notifications_jsonl_path`` are kept untouched (the sink imports them).

The implementation does not exist yet on this branch, so this suite is
intentionally RED until a follow-up dispatch implements it.
"""
import ast
import json
import logging
import re
from pathlib import Path

import pytest

from pipeline import persistence


# --------------------------------------------------------------------------- #
# Shared fixture: tmp_path-backed PLAN_DIR (never the real ~/.claude/plans).
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point ``persistence.PLAN_DIR`` (and ``paths.PLAN_DIR``) at tmp_path."""
    from pipeline import paths

    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1 & 2: behavior-preserving refactor -- the on-disk artifacts are unchanged.
# --------------------------------------------------------------------------- #
def test_log_line_is_unchanged_after_refactor(plan_dir):
    """A plain ``_notify_user("p", "hello")`` still appends exactly one line
    matching ``^\\S+ hello$`` to ``p.notifications.log``."""
    persistence._notify_user("p", "hello")
    log_path = plan_dir / "p.notifications.log"
    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1, f"expected exactly one log line, got {lines!r}"
    assert re.match(r"^\S+ hello$", lines[0]), (
        f"log line must be '<ISO ts> hello', got {lines[0]!r}"
    )


def test_jsonl_record_is_unchanged_after_refactor(plan_dir):
    """The same call still appends one JSONL record with severity ``info`` and
    null ``story_key``/``event``/``dedup_key``."""
    persistence._notify_user("p", "hello")
    jsonl_path = plan_dir / "p.notifications.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected exactly one JSONL line, got {lines!r}"
    record = json.loads(lines[0])
    assert record["severity"] == "info"
    assert record["story_key"] is None
    assert record["event"] is None
    assert record["dedup_key"] is None
    assert record["message"] == "hello"
    assert record["plan"] == "p"


# --------------------------------------------------------------------------- #
# 3: publish is called exactly once with a well-formed notification event.
# --------------------------------------------------------------------------- #
class _RecordingBus:
    """A bus double that records every published event and never raises."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def test_publish_is_called_exactly_once(plan_dir, monkeypatch):
    """Patch ``get_bus`` to return a recording double; assert ``publish`` was
    called once with an event whose ``type == "notification"`` and whose
    payload carries ``message``/``severity``/``story_key``/``event``/
    ``dedup_key``."""
    from pipeline import event_wiring

    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: bus)

    persistence._notify_user("p", "hello")

    assert len(bus.published) == 1, (
        f"expected exactly one publish, got {len(bus.published)}"
    )
    evt = bus.published[0]
    assert evt["type"] == "notification"
    payload = evt["payload"]
    assert payload["message"] == "hello"
    assert payload["severity"] == "info"
    assert payload["story_key"] is None
    assert payload["event"] is None
    assert payload["dedup_key"] is None
    # The event must carry the plan and a timestamp.
    assert evt["plan"] == "p"
    assert evt.get("ts")
    assert payload["ts"] == evt["ts"]


# --------------------------------------------------------------------------- #
# 4: bus failure falls back to direct write + ERROR log.
# --------------------------------------------------------------------------- #
def test_bus_failure_falls_back_to_direct_write(plan_dir, monkeypatch, caplog):
    """Patch ``pipeline.event_wiring.get_bus`` to raise ``RuntimeError``;
    ``_notify_user`` still writes BOTH files, returns ``None``, and logs at
    ERROR."""
    from pipeline import event_wiring

    def boom():
        raise RuntimeError("bus unavailable")

    monkeypatch.setattr(event_wiring, "get_bus", boom)

    with caplog.at_level(logging.ERROR, logger="pipeline.persistence"):
        result = persistence._notify_user("p", "hello")

    # Returns None (never raises).
    assert result is None
    # Both files written directly.
    log_path = plan_dir / "p.notifications.log"
    jsonl_path = plan_dir / "p.notifications.jsonl"
    assert log_path.exists(), "fallback must write the free-text log"
    assert jsonl_path.exists(), "fallback must write the JSONL sidecar"
    log_lines = log_path.read_text().splitlines()
    assert len(log_lines) == 1
    assert re.match(r"^\S+ hello$", log_lines[0])
    record = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["message"] == "hello"
    assert record["severity"] == "info"
    # Logged at ERROR.
    assert any(rec.levelno == logging.ERROR for rec in caplog.records), (
        f"expected an ERROR log on bus failure, got {caplog.records!r}"
    )


def test_bus_publish_failure_falls_back_to_direct_write(plan_dir, monkeypatch, caplog):
    """A ``publish()`` that raises (bus machinery failure, not a sink failure)
    must also fall back to direct write + ERROR log."""
    from pipeline import event_wiring

    class _PublishBoomBus:
        def publish(self, event):
            raise RuntimeError("publish exploded")

    monkeypatch.setattr(event_wiring, "get_bus", lambda: _PublishBoomBus())

    with caplog.at_level(logging.ERROR, logger="pipeline.persistence"):
        result = persistence._notify_user("p", "hello")

    assert result is None
    assert (plan_dir / "p.notifications.log").exists()
    assert (plan_dir / "p.notifications.jsonl").exists()
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_lazy_import_failure_falls_back_to_direct_write(plan_dir, monkeypatch, caplog):
    """If the lazy ``from .event_wiring import get_bus`` import itself fails,
    the fallback still writes both files and logs at ERROR.

    We simulate an import failure by making ``event_wiring`` unimportable via
    ``sys.modules`` manipulation, which forces the lazy import inside
    ``_notify_user`` to raise ``ImportError``.
    """
    import sys

    # Setting a sys.modules entry to None is the documented CPython way to
    # make a subsequent ``import`` of that name raise ImportError ("import of
    # pipeline.event_wiring halted; None in sys.modules"). This forces the
    # lazy ``from .event_wiring import get_bus`` inside ``_notify_user`` to
    # fail, exercising the import-error branch of the fallback.
    real = sys.modules.get("pipeline.event_wiring")
    sys.modules["pipeline.event_wiring"] = None
    try:
        with caplog.at_level(logging.ERROR, logger="pipeline.persistence"):
            result = persistence._notify_user("p", "hello")
    finally:
        if real is not None:
            sys.modules["pipeline.event_wiring"] = real
        else:
            sys.modules.pop("pipeline.event_wiring", None)

    assert result is None
    assert (plan_dir / "p.notifications.log").exists()
    assert (plan_dir / "p.notifications.jsonl").exists()
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


# --------------------------------------------------------------------------- #
# 5: no module-level event_wiring import (the cycle guard).
# --------------------------------------------------------------------------- #
def _persistence_source() -> str:
    p = Path("pipeline/persistence.py")
    assert p.exists(), "pipeline/persistence.py must exist"
    return p.read_text(encoding="utf-8")


def test_no_module_level_event_wiring_import():
    """Parse ``pipeline/persistence.py`` with ``ast`` and assert there is no
    module-level ``Import``/``ImportFrom`` naming ``pipeline.event_wiring`` or
    ``.event_wiring`` (the cycle guard). Modeled on the AST import check in
    ``tests/unit/test_config_provenance.py``."""
    tree = ast.parse(_persistence_source())
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "pipeline.event_wiring", (
                    "persistence must not import pipeline.event_wiring at module level"
                )
                assert not alias.name.startswith("pipeline.event_wiring."), (
                    "persistence must not import pipeline.event_wiring at module level"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module != "pipeline.event_wiring", (
                "persistence must not import from pipeline.event_wiring at module level"
            )
            assert not module.startswith("pipeline.event_wiring."), (
                "persistence must not import from pipeline.event_wiring at module level"
            )
            # Relative import of .event_wiring (level >= 1, module == "event_wiring").
            if node.level and node.level >= 1:
                assert module != "event_wiring", (
                    "persistence must not import from .event_wiring at module level"
                )


def test_lazy_import_comment_present():
    """The lazy import inside ``_notify_user`` must carry a comment explaining
    WHY it is lazy (the import cycle). The story explicitly requires this."""
    src = _persistence_source()
    # The function body must contain a lazy import of get_bus from .event_wiring
    # AND a comment mentioning the cycle / lazy reason.
    assert "from .event_wiring import get_bus" in src, (
        "_notify_user must lazily import get_bus from .event_wiring inside the function"
    )
    # A comment near the import referencing the cycle / lazy reason.
    assert re.search(r"#.*cycle|#.*lazy|#.*circular", src, re.IGNORECASE), (
        "the lazy import must be documented with a comment about the import cycle"
    )


def test_no_double_write_comment_present():
    """The story requires a comment explaining why the fallback cannot
    double-write (InProcessEventBus.publish swallows handler exceptions)."""
    src = _persistence_source()
    assert re.search(r"swallow|double.write|no sink ran|exactly one write", src, re.IGNORECASE), (
        "the fallback branch must document why it cannot double-write"
    )


# --------------------------------------------------------------------------- #
# 6: keyword fields survive the bus.
# --------------------------------------------------------------------------- #
def test_keyword_fields_survive_the_bus(plan_dir, monkeypatch):
    """``_notify_user("p", "m", story_key="S1", severity="warning",
    event="ci_pending_stalled", dedup_key="k")`` produces a JSONL record with
    all four values intact after going through publish."""
    from pipeline import event_wiring

    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: bus)

    persistence._notify_user(
        "p",
        "m",
        story_key="S1",
        severity="warning",
        event="ci_pending_stalled",
        dedup_key="k",
    )

    assert len(bus.published) == 1
    evt = bus.published[0]
    payload = evt["payload"]
    assert payload["story_key"] == "S1"
    assert payload["severity"] == "warning"
    assert payload["event"] == "ci_pending_stalled"
    assert payload["dedup_key"] == "k"
    assert payload["message"] == "m"
    # The event-level story_key must also reflect the keyword arg.
    assert evt["story_key"] == "S1"
    # And the JSONL record on disk (written by the sink via the bus) carries
    # all four values intact.
    record = json.loads(
        (plan_dir / "p.notifications.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["story_key"] == "S1"
    assert record["severity"] == "warning"
    assert record["event"] == "ci_pending_stalled"
    assert record["dedup_key"] == "k"


# --------------------------------------------------------------------------- #
# Signature preservation: keyword-only params with the same defaults.
# --------------------------------------------------------------------------- #
def test_signature_unchanged_keyword_only_defaults():
    """The signature of ``_notify_user`` must stay exactly as the previous
    story left it: two positional params, then keyword-only
    ``story_key``/``severity``/``event``/``dedup_key`` with defaults
    ``None``/``"info"``/``None``/``None``, extended by this story with the
    correlation/context params (all keyword-only, default ``None``)."""
    import inspect

    sig = inspect.signature(persistence._notify_user)
    params = list(sig.parameters.values())
    # Exactly: plan_name, message, *, story_key, severity, event, dedup_key,
    # correlation_id, attempt, role, provider, model.
    assert [p.name for p in params] == [
        "plan_name",
        "message",
        "story_key",
        "severity",
        "event",
        "dedup_key",
        "correlation_id",
        "attempt",
        "role",
        "provider",
        "model",
    ]
    # story_key/event/dedup_key default to None; severity defaults to "info".
    assert params[2].default is None  # story_key
    assert params[3].default == "info"  # severity
    assert params[4].default is None  # event
    assert params[5].default is None  # dedup_key
    # The correlation/context params added by W4L-01 default to None.
    for p in params[6:]:
        assert p.default is None, f"{p.name} must default to None"
    # story_key..dedup_key are keyword-only (KIND is KEYWORD_ONLY).
    for p in params[2:]:
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{p.name} must be keyword-only"
        )
    # plan_name and message are positional-or-keyword.
    for p in params[:2]:
        assert p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_third_positional_argument_still_rejected(plan_dir):
    """The two-positional-argument contract is preserved: a third positional
    arg must still raise ``TypeError``."""
    with pytest.raises(TypeError):
        persistence._notify_user("p", "m", "S1")


# --------------------------------------------------------------------------- #
# Helpers preserved: the sink imports them, so they must stay untouched.
# --------------------------------------------------------------------------- #
def test_helpers_still_present_and_callable():
    """``_write_notification_record``, ``_notification_record`` and
    ``_notifications_jsonl_path`` must remain present (the sink imports them)."""
    assert callable(persistence._write_notification_record)
    assert callable(persistence._notification_record)
    assert callable(persistence._notifications_jsonl_path)


def test_helpers_in_all():
    """All three helpers must remain exported in ``__all__``."""
    for name in (
        "_write_notification_record",
        "_notification_record",
        "_notifications_jsonl_path",
        "_notify_user",
    ):
        assert name in persistence.__all__, f"{name} must remain in __all__"


# --------------------------------------------------------------------------- #
# Severity fallback stays single-sourced through the bus payload.
# --------------------------------------------------------------------------- #
def test_invalid_severity_falls_back_to_info_through_bus(plan_dir, monkeypatch):
    """An invalid severity must still fall back to ``info`` (single-sourced in
    ``_notification_record``) and the bus payload must carry the corrected
    ``"info"`` severity, not the invalid value."""
    from pipeline import event_wiring

    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: bus)

    persistence._notify_user("p", "m", severity="catastrophic")

    assert len(bus.published) == 1
    payload = bus.published[0]["payload"]
    assert payload["severity"] == "info", (
        "bus payload severity must reflect the record's corrected severity"
    )