"""Tests for the plan outbox notification sink (``pipeline.notification_outbox``).

Written FIRST (TDD) for the notification-outbox story.  The implementation does
not exist yet, so the module-level import of ``outbox_sink`` fails with an
import error and every test below errors -- that is the intended RED state for
this dispatch.

Contract under test (see REFERENCE.md, "When writing a new sink"):

* ``outbox_sink(event) -> None`` spools *selected* ``notification`` bus events
  to ``PLAN_DIR / f"{plan}.outbox.jsonl"`` as one JSON line per notification,
  for a later out-of-band send.  It performs NO network I/O.
* Disabled by default: unless ``PIPELINE_NOTIFY_OUTBOX_ENABLED`` is exactly
  ``"1"``, the sink writes nothing and does not even create the file.
* Event allowlist: only events whose structured ``payload["event"]`` is in the
  comma-separated ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` list (default
  ``"plan_completed"``) are spooled.  Message text is never matched.
* Retention is delegated to ``persistence._rotate_if_needed(path,
  persistence.NOTIFICATIONS_MAX_BYTES, persistence.NOTIFICATIONS_KEEP_N)``
  before appending, exactly like ``file_log_sink``, so the policy cannot drift.
* The sink never raises: every failure is logged at ERROR with ``exc_info``
  and swallowed, and a failing outbox sink never prevents ``file_log_sink``
  from writing its own record.
* ``build_bus()`` registers ``outbox_sink`` on ``notification``, after
  ``file_log_sink`` (whose ordering must not change).  Assertions on the
  handler list are membership/order based, never exact list equality: the
  list is cumulative and later stories may register further sinks.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from pathlib import Path

import pytest
from pipeline.notification_outbox import outbox_sink

from pipeline import event_wiring, notification_outbox, paths, persistence
from pipeline.event_wiring import build_bus, get_bus
from pipeline.events import make_event
from pipeline.notification_sinks import file_log_sink

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _patch_plan_dir(monkeypatch, target):
    """Point ``PLAN_DIR`` at ``target`` everywhere a sink may read it.

    ``persistence`` re-binds ``PLAN_DIR`` from ``.paths`` at import time and
    the sinks read it at call time; patch the canonical location, the
    re-bound name, and (if present) the outbox module's own re-binding.
    """
    monkeypatch.setattr(paths, "PLAN_DIR", target)
    monkeypatch.setattr(persistence, "PLAN_DIR", target)
    if hasattr(notification_outbox, "PLAN_DIR"):
        monkeypatch.setattr(notification_outbox, "PLAN_DIR", target)


@pytest.fixture
def outbox_env(tmp_path, monkeypatch):
    """Isolated ``PLAN_DIR`` plus a clean notification-outbox environment.

    Every env var the sink reads is removed first, so no test depends on the
    ambient environment; each test then sets exactly what it needs via
    ``monkeypatch.setenv``.
    """
    _patch_plan_dir(monkeypatch, tmp_path)
    monkeypatch.delenv(OUTBOX_ENABLED_ENV, raising=False)
    monkeypatch.delenv(OUTBOX_EVENTS_ENV, raising=False)
    return tmp_path


def _notif(plan="p1", payload=None):
    """Build a realistic ``notification`` bus event."""
    if payload is None:
        payload = {"message": "plan finished", "event": "plan_completed"}
    return make_event("notification", plan, payload=payload)


def _outbox_path(plan_dir, plan="p1"):
    return Path(plan_dir) / f"{plan}.outbox.jsonl"


def _json_lines(path):
    """Return the non-empty lines of a JSONL file as a list of strings."""
    return [line for line in path.read_text().splitlines() if line.strip()]


def _contains(obj, needle):
    """Recursively check that ``needle`` appears anywhere in a JSON object."""
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(
            _contains(key, needle) or _contains(value, needle)
            for key, value in obj.items()
        )
    if isinstance(obj, (list, tuple)):
        return any(_contains(item, needle) for item in obj)
    return False


# ---------------------------------------------------------------------------
# Signature / module contract
# ---------------------------------------------------------------------------

def test_outbox_sink_takes_a_single_event_argument_and_returns_none():
    """``outbox_sink(event: dict) -> None``: one positional arg, annotated None."""
    assert callable(outbox_sink)
    signature = inspect.signature(outbox_sink)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    assert len(positional) == 1, (
        f"outbox_sink must take exactly one positional event argument, "
        f"got parameters {list(signature.parameters)}"
    )
    assert signature.return_annotation in (None, "None", type(None)), (
        "outbox_sink must be annotated '-> None'"
    )


def test_outbox_module_imports_no_network_libraries():
    """The outbox sink performs no network I/O: no network client import."""
    source = Path(notification_outbox.__file__).read_text(encoding="utf-8")
    banned = {
        "smtplib", "email", "socket", "urllib", "http", "requests",
        "ftplib", "poplib", "imaplib", "telnetlib", "nntplib", "xmlrpc",
    }
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""] if node.level == 0 else []
        else:
            continue
        for name in imported:
            root = name.split(".")[0]
            assert root not in banned, (
                f"notification_outbox must not import network library {name!r}"
            )


# ---------------------------------------------------------------------------
# Positive: enabled + allowlisted events are spooled
# ---------------------------------------------------------------------------

def test_enabled_allowlisted_event_appends_one_round_trippable_json_line(
    outbox_env, monkeypatch
):
    """Enabled + default allowlist -> exactly one JSON line whose parsed
    object round-trips the payload fields."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    payload = {
        "message": "plan finished",
        "event": "plan_completed",
        "story_key": "S1",
    }

    result = outbox_sink(_notif(payload=payload))

    assert result is None, "outbox_sink must return None"
    path = _outbox_path(outbox_env)
    assert path.exists(), "outbox file was not created for an enabled, allowlisted event"
    lines = _json_lines(path)
    assert len(lines) == 1, f"expected exactly one JSON line, got {len(lines)}"
    record = json.loads(lines[0])  # must be valid JSON
    assert isinstance(record, dict)
    # The payload fields must round-trip: the later out-of-band send needs them.
    assert _contains(record, "plan finished"), lines[0]
    assert _contains(record, "plan_completed"), lines[0]
    assert _contains(record, "S1"), lines[0]


def test_two_notifications_append_two_lines_in_order(outbox_env, monkeypatch):
    """Two spooled notifications -> two JSON lines, in dispatch order."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    outbox_sink(_notif(payload={"message": "first", "event": "plan_completed"}))
    outbox_sink(_notif(payload={"message": "second", "event": "plan_completed"}))

    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 2, f"expected two JSON lines, got {len(lines)}"
    assert "first" in lines[0]
    assert "second" in lines[1]


def test_outbox_appends_to_existing_file_instead_of_overwriting(
    outbox_env, monkeypatch
):
    """A pre-existing outbox file must be appended to, never truncated."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    path = _outbox_path(outbox_env)
    path.write_text('{"seed": true}\n')

    outbox_sink(_notif(payload={"message": "appended", "event": "plan_completed"}))

    lines = _json_lines(path)
    assert len(lines) == 2, f"expected the seed line plus one append, got {len(lines)}"
    assert json.loads(lines[0]) == {"seed": True}
    assert "appended" in lines[1]


# ---------------------------------------------------------------------------
# Wiring: build_bus / get_bus register the outbox sink
# ---------------------------------------------------------------------------

def test_build_bus_registers_outbox_sink_on_notification():
    """``build_bus`` must register ``outbox_sink`` on ``notification``.

    Membership, not list equality: the handler list is cumulative and a later
    story may register further sinks on the same topic.
    """
    event_wiring._BUS = None
    bus = build_bus()
    handlers = bus._handlers["notification"]
    assert outbox_sink in handlers
    assert handlers.count(outbox_sink) == 1


def test_build_bus_keeps_file_log_sink_subscribed_before_outbox_sink():
    """``file_log_sink`` must stay subscribed before ``outbox_sink``."""
    event_wiring._BUS = None
    bus = build_bus()
    handlers = bus._handlers["notification"]
    assert file_log_sink in handlers
    assert outbox_sink in handlers
    assert handlers.index(file_log_sink) < handlers.index(outbox_sink)


def test_get_bus_does_not_duplicate_outbox_sink():
    """Repeated ``get_bus`` calls must not subscribe ``outbox_sink`` twice."""
    event_wiring._BUS = None
    get_bus()
    get_bus()
    bus = get_bus()
    assert bus._handlers["notification"].count(outbox_sink) == 1


# ---------------------------------------------------------------------------
# Negative / boundary: the secure default and the event allowlist
# ---------------------------------------------------------------------------

def test_disabled_by_default_when_env_unset(outbox_env):
    """Secure default: env unset -> nothing written, file not even created."""
    outbox_sink(_notif())
    assert not _outbox_path(outbox_env).exists(), (
        "outbox must be disabled by default (PIPELINE_NOTIFY_OUTBOX_ENABLED unset)"
    )


def test_disabled_when_env_is_zero(outbox_env, monkeypatch):
    """``PIPELINE_NOTIFY_OUTBOX_ENABLED=0`` -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "0")
    outbox_sink(_notif())
    assert not _outbox_path(outbox_env).exists()


def test_disabled_when_env_is_not_exactly_one(outbox_env, monkeypatch):
    """Only the exact string ``"1"`` enables the sink; ``"true"`` must not."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "true")
    outbox_sink(_notif())
    assert not _outbox_path(outbox_env).exists()


def test_enabled_but_event_not_in_default_allowlist(outbox_env, monkeypatch):
    """Enabled, but ``payload['event']`` not allowlisted -> nothing written.

    The default allowlist is ``plan_completed``; e.g. every dispatch-retry
    notice must NOT be queued for delivery.
    """
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    outbox_sink(_notif(payload={"message": "merged story", "event": "story_merged"}))
    assert not _outbox_path(outbox_env).exists()


def test_enabled_but_payload_event_missing_none_or_payload_absent(
    outbox_env, monkeypatch
):
    """Enabled, but no structured ``payload['event']`` -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    outbox_sink(_notif(payload={"message": "no event key"}))
    outbox_sink(_notif(payload={"message": "none event", "event": None}))
    outbox_sink({"type": "notification", "ts": "2024-01-01T00:00:00+00:00", "plan": "p1"})
    # Control: with the same settings an allowlisted event must be written,
    # proving the rejections above were allowlist-driven, not a dead sink.
    outbox_sink(_notif(payload={"message": "control", "event": "plan_completed"}))
    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 1, f"only the control event must be spooled, got {lines}"
    assert "control" in lines[0]


def test_custom_allowlist_admits_listed_events_and_rejects_others(
    outbox_env, monkeypatch
):
    """``PIPELINE_NOTIFY_OUTBOX_EVENTS='a,b'`` admits 'a' and 'b' and rejects
    the default 'plan_completed'."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(OUTBOX_EVENTS_ENV, "a,b")
    outbox_sink(_notif(payload={"message": "m1", "event": "a"}))
    outbox_sink(_notif(payload={"message": "m2", "event": "b"}))
    outbox_sink(_notif(payload={"message": "m3", "event": "plan_completed"}))

    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 2, f"expected exactly the 'a' and 'b' records, got {lines}"
    assert "m1" in lines[0]
    assert "m2" in lines[1]
    assert all("m3" not in line for line in lines)
    assert all("plan_completed" not in line for line in lines)


# ---------------------------------------------------------------------------
# Negative / boundary: malformed events and failing writes never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_event",
    [
        # "plan" key missing entirely
        {
            "type": "notification",
            "ts": "2024-01-01T00:00:00+00:00",
            "payload": {"message": "x", "event": "plan_completed"},
        },
        # "plan" explicitly empty
        {
            "type": "notification",
            "ts": "2024-01-01T00:00:00+00:00",
            "plan": "",
            "payload": {"message": "x", "event": "plan_completed"},
        },
        # "plan" explicitly None
        {
            "type": "notification",
            "ts": "2024-01-01T00:00:00+00:00",
            "plan": None,
            "payload": {"message": "x", "event": "plan_completed"},
        },
    ],
)
def test_missing_or_falsy_plan_logs_error_and_writes_nothing(
    outbox_env, monkeypatch, caplog, bad_event
):
    """A missing/falsy ``plan`` -> no write, no raise, ERROR logged."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    with caplog.at_level(logging.ERROR):
        outbox_sink(bad_event)  # must not raise
    assert not _outbox_path(outbox_env).exists()
    assert any(
        record.levelno == logging.ERROR for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_unwritable_outbox_path_never_raises_and_logs_error(
    outbox_env, monkeypatch, caplog
):
    """PLAN_DIR pointed at a regular file -> no raise escapes, ERROR logged
    with ``exc_info`` set."""
    blocker = outbox_env / "plan_dir_is_a_file"
    blocker.write_text("not a directory")
    _patch_plan_dir(monkeypatch, blocker)
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    with caplog.at_level(logging.ERROR):
        outbox_sink(_notif())  # must not raise

    assert not (blocker / "p1.outbox.jsonl").exists()
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors, "an unwritable outbox path must be logged at ERROR"
    assert any(
        record.exc_info is not None for record in errors
    ), "the ERROR log must carry exc_info=True"


def test_rotation_failure_never_raises_and_logs_error(outbox_env, monkeypatch, caplog):
    """If ``persistence._rotate_if_needed`` raises, the sink swallows it."""
    def boom(path, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(persistence, "_rotate_if_needed", boom)
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    with caplog.at_level(logging.ERROR):
        outbox_sink(_notif())  # must not raise

    assert not _outbox_path(outbox_env).exists()
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors, "a rotation failure must be logged at ERROR"
    assert any(
        record.exc_info is not None for record in errors
    ), "the ERROR log must carry exc_info=True"


def test_outbox_delegates_retention_to_persistence_rotate_if_needed(
    outbox_env, monkeypatch
):
    """The sink must reuse ``persistence._rotate_if_needed(path,
    NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)`` before appending, exactly
    like ``file_log_sink``, so the retention policy cannot drift."""
    calls = []
    real_rotate = persistence._rotate_if_needed

    def spy(path, *args, **kwargs):
        calls.append((path, args, kwargs))
        return real_rotate(path, *args, **kwargs)

    monkeypatch.setattr(persistence, "_rotate_if_needed", spy)
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(_notif())

    assert calls, "outbox_sink must call persistence._rotate_if_needed"
    path, args, kwargs = calls[0]
    assert Path(path) == _outbox_path(outbox_env), (
        f"rotation must target the plan outbox file, got {path}"
    )
    values = list(args) + list(kwargs.values())
    assert persistence.NOTIFICATIONS_MAX_BYTES in values, (
        "rotation must be called with persistence.NOTIFICATIONS_MAX_BYTES"
    )
    assert persistence.NOTIFICATIONS_KEEP_N in values, (
        "rotation must be called with persistence.NOTIFICATIONS_KEEP_N"
    )
    # The write itself still happened after the rotation call.
    assert len(_json_lines(_outbox_path(outbox_env))) == 1


def test_raising_outbox_sink_does_not_prevent_file_log_sink(
    outbox_env, monkeypatch
):
    """Defence in depth: when ``outbox_sink`` fails, ``file_log_sink``
    (subscribed first on the same bus) still writes its record."""
    real_rotate = persistence._rotate_if_needed

    def selective_boom(path, *args, **kwargs):
        if str(path).endswith(".outbox.jsonl"):
            raise OSError("outbox unwritable")
        return real_rotate(path, *args, **kwargs)

    monkeypatch.setattr(persistence, "_rotate_if_needed", selective_boom)
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    event_wiring._BUS = None
    bus = build_bus()
    bus.publish(_notif())  # must not raise

    assert (outbox_env / "p1.notifications.log").exists(), (
        "file_log_sink must still write its record when outbox_sink fails"
    )
    assert not _outbox_path(outbox_env).exists(), (
        "the failing outbox write must be contained (no partial outbox file)"
    )