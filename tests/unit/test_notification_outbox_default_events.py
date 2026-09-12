"""Tests for the DEFAULT allowlist of the plan outbox notification sink.

Written FIRST (TDD) for NOTIFYLC-2: the default value of
``PIPELINE_NOTIFY_OUTBOX_EVENTS`` must cover the parked/failed story events
(``story_parked``, ``dispatch_failed``, ``tests_failed``, ``agent_gave_up``)
in addition to the existing ``plan_completed`` completion notice, so the
operator is e-mailed whenever a story parks or fails and needs a human.

Contract under test (behaviour, not the constant's exact total contents):

* With ``PIPELINE_NOTIFY_OUTBOX_ENABLED=1`` and ``PIPELINE_NOTIFY_OUTBOX_EVENTS``
  unset, each of the five default events is spooled.
* ``story_merged`` (a healthy progress event) is NOT spooled.
* A missing ``event`` key, an ``event`` of ``None``, and an absent ``payload``
  are NOT spooled -- and never raise.
* An explicit ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` REPLACES the default entirely
  (no union): with ``"a,b"``, ``plan_completed`` is rejected again.
* With the outbox disabled, nothing is spooled.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import notification_outbox, paths, persistence
from pipeline.events import make_event
from pipeline.notification_outbox import outbox_sink

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"

# The five events the default allowlist must admit.  Membership is asserted
# per event; no test below asserts equality against the whole
# DEFAULT_OUTBOX_EVENTS string or its length, so a later story that
# legitimately adds a sixth default does not have to edit this file.
DEFAULT_EVENTS = [
    "plan_completed",
    "story_parked",
    "dispatch_failed",
    "tests_failed",
    "agent_gave_up",
]


# ---------------------------------------------------------------------------
# Helpers / fixtures (mirroring tests/unit/test_notification_outbox_sink.py)
# ---------------------------------------------------------------------------

def _patch_plan_dir(monkeypatch, target):
    """Point ``PLAN_DIR`` at ``target`` everywhere a sink may read it."""
    monkeypatch.setattr(paths, "PLAN_DIR", target)
    monkeypatch.setattr(persistence, "PLAN_DIR", target)
    if hasattr(notification_outbox, "PLAN_DIR"):
        monkeypatch.setattr(notification_outbox, "PLAN_DIR", target)


@pytest.fixture
def outbox_env(tmp_path, monkeypatch):
    """Isolated ``PLAN_DIR`` plus a clean notification-outbox environment."""
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


# ---------------------------------------------------------------------------
# Positive: each default event is spooled when EVENTS is unset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evt_name", DEFAULT_EVENTS)
def test_default_allowlist_spools_each_default_event(outbox_env, monkeypatch, evt_name):
    """Enabled with EVENTS unset: ``payload['event'] == <E>`` IS spooled."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    assert OUTBOX_EVENTS_ENV not in __import__("os").environ

    outbox_sink(_notif(payload={"message": f"about {evt_name}", "event": evt_name}))

    path = _outbox_path(outbox_env)
    assert path.exists(), f"event {evt_name!r} must be spooled (outbox file missing)"
    lines = _json_lines(path)
    assert len(lines) == 1, f"event {evt_name!r} must be spooled, got {lines}"
    assert evt_name in lines[0]


# ---------------------------------------------------------------------------
# Negative / boundary
# ---------------------------------------------------------------------------

def test_default_allowlist_rejects_story_merged(outbox_env, monkeypatch):
    """Healthy progress events stay out: ``story_merged`` is NOT spooled."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(_notif(payload={"message": "merged story", "event": "story_merged"}))

    assert not _outbox_path(outbox_env).exists()


def test_default_allowlist_rejects_event_none(outbox_env, monkeypatch):
    """``payload['event'] is None`` is not allowlisted -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(_notif(payload={"message": "none event", "event": None}))

    assert not _outbox_path(outbox_env).exists()


def test_default_allowlist_rejects_missing_event_key(outbox_env, monkeypatch):
    """A payload without an ``event`` key is not allowlisted -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(_notif(payload={"message": "no event key"}))

    assert not _outbox_path(outbox_env).exists()


def test_default_allowlist_rejects_absent_payload(outbox_env, monkeypatch):
    """A record with no ``payload`` key at all is not allowlisted -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(
        {"type": "notification", "ts": "2024-01-01T00:00:00+00:00", "plan": "p1"}
    )

    assert not _outbox_path(outbox_env).exists()


def test_malformed_payloads_never_raise_and_sink_stays_usable(
    outbox_env, monkeypatch
):
    """Skipped malformed calls must not raise or break the sink for later calls."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")

    outbox_sink(_notif(payload={"message": "none event", "event": None}))
    outbox_sink(_notif(payload={"message": "no event key"}))
    outbox_sink(
        {"type": "notification", "ts": "2024-01-01T00:00:00+00:00", "plan": "p1"}
    )
    assert not _outbox_path(outbox_env).exists()

    # Control: the sink still works after the skipped calls.
    outbox_sink(_notif(payload={"message": "control", "event": "plan_completed"}))
    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 1, f"only the control event must be spooled, got {lines}"
    assert "control" in lines[0]


# ---------------------------------------------------------------------------
# Override: an explicit EVENTS value REPLACES the default entirely
# ---------------------------------------------------------------------------

def test_explicit_events_env_replaces_default_entirely(outbox_env, monkeypatch):
    """``EVENTS='a,b'`` must REPLACE the default: 'plan_completed' is rejected."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(OUTBOX_EVENTS_ENV, "a,b")

    outbox_sink(_notif(payload={"message": "m1", "event": "plan_completed"}))
    assert not _outbox_path(outbox_env).exists(), (
        "an explicit EVENTS value must replace the default entirely, "
        "not union with it"
    )

    outbox_sink(_notif(payload={"message": "m2", "event": "a"}))
    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 1, f"'a' must be spooled, got {lines}"
    assert "m2" in lines[0]

    outbox_sink(_notif(payload={"message": "m3", "event": "b"}))
    lines = _json_lines(_outbox_path(outbox_env))
    assert len(lines) == 2, f"'b' must be spooled too, got {lines}"
    assert "m3" in lines[1]


# ---------------------------------------------------------------------------
# Disabled: nothing is spooled, not even the default events
# ---------------------------------------------------------------------------

def test_disabled_outbox_spools_nothing(outbox_env, monkeypatch):
    """``ENABLED=0`` with EVENTS unset: none of the five defaults is spooled."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "0")

    outbox_sink(_notif(payload={"message": "done", "event": "plan_completed"}))
    assert not _outbox_path(outbox_env).exists()

    outbox_sink(_notif(payload={"message": "parked", "event": "story_parked"}))
    assert not _outbox_path(outbox_env).exists()

    outbox_sink(_notif(payload={"message": "failed", "event": "dispatch_failed"}))
    assert not _outbox_path(outbox_env).exists()

    outbox_sink(_notif(payload={"message": "red", "event": "tests_failed"}))
    assert not _outbox_path(outbox_env).exists()

    outbox_sink(_notif(payload={"message": "gave up", "event": "agent_gave_up"}))
    assert not _outbox_path(outbox_env).exists()