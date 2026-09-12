"""Tests for the default outbox event allowlist (``DEFAULT_OUTBOX_EVENTS``).

Written FIRST (TDD) for the "widen the default allowlist so a parked story can
be e-mailed" story.  The implementation does not exist yet: today
``DEFAULT_OUTBOX_EVENTS`` is only ``"plan_completed"``, so every positive
assertion below fails on a missing spooled record -- that is the intended RED
state for this dispatch.

Contract under test (``pipeline/notification_outbox.py``):

* ``DEFAULT_OUTBOX_EVENTS`` is the allowlist used when
  ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` is unset.  It must cover the events that
  mean "a human is needed": ``plan_completed``, ``story_parked``,
  ``dispatch_failed``, ``tests_failed`` and ``agent_gave_up``.
* Healthy-progress events (``story_merged``, ``rebase_auto_resolved``,
  ``merge_retry``, ...) stay OUT of the default: the operator is only mailed
  when something needs them.
* An explicit ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` replaces the default entirely.
* The module docstring's selection-rules bullet must state the same default as
  the code.

Assertions are membership/spooling based, never an equality check against the
whole ``DEFAULT_OUTBOX_EVENTS`` string or its length: a later story that
legitimately adds a sixth default must not have to edit this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import notification_outbox, paths, persistence
from pipeline.events import make_event
from pipeline.notification_outbox import outbox_sink

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"

# The five events this story requires the default allowlist to cover.  Kept as
# a local tuple (not read from the module) so the test states the requirement
# independently of the implementation.
REQUIRED_DEFAULT_EVENTS = (
    "plan_completed",
    "story_parked",
    "dispatch_failed",
    "tests_failed",
    "agent_gave_up",
)

# Healthy-progress events that must stay OUT of the default allowlist: the
# operator is only mailed when something needs them.
HEALTHY_PROGRESS_EVENTS = ("story_merged", "rebase_auto_resolved", "merge_retry")

# The single healthy-progress event used for the spooling-rejection test.
HEALTHY_PROGRESS_EVENT = "story_merged"


# ---------------------------------------------------------------------------
# Helpers / fixtures
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


def _notif(plan, event_name, message="notice"):
    """Build a realistic ``notification`` bus event for ``event_name``."""
    return make_event(
        "notification", plan, payload={"message": message, "event": event_name}
    )


def _outbox_path(plan_dir, plan):
    return Path(plan_dir) / f"{plan}.outbox.jsonl"


def _json_lines(path):
    """Return the non-empty lines of a JSONL file as a list of strings."""
    return [line for line in path.read_text().splitlines() if line.strip()]


def _spooled_events(plan_dir, plan):
    """Return the ``payload['event']`` values spooled for ``plan``."""
    path = _outbox_path(plan_dir, plan)
    if not path.exists():
        return []
    return [json.loads(line)["payload"]["event"] for line in _json_lines(path)]


def _default_allowlist():
    """Parse ``DEFAULT_OUTBOX_EVENTS`` the same way the sink does."""
    raw = notification_outbox.DEFAULT_OUTBOX_EVENTS
    assert isinstance(raw, str), (
        f"DEFAULT_OUTBOX_EVENTS must stay a comma-separated string, got {raw!r}"
    )
    return {name.strip() for name in raw.split(",") if name.strip()}


# ---------------------------------------------------------------------------
# The constant itself
# ---------------------------------------------------------------------------

def test_default_outbox_events_is_a_comma_separated_string():
    """The default stays a plain comma-separated string (env-var shaped)."""
    raw = notification_outbox.DEFAULT_OUTBOX_EVENTS
    assert isinstance(raw, str)
    assert raw.strip(), "DEFAULT_OUTBOX_EVENTS must not be empty"


@pytest.mark.parametrize("event_name", REQUIRED_DEFAULT_EVENTS)
def test_default_allowlist_contains_each_required_event(event_name):
    """Membership only -- never an equality check on the whole string."""
    assert event_name in _default_allowlist(), (
        f"{event_name!r} must be part of the default outbox allowlist"
    )


@pytest.mark.parametrize("event_name", HEALTHY_PROGRESS_EVENTS)
def test_default_allowlist_excludes_healthy_progress_events(event_name):
    """Healthy progress must not page the operator."""
    assert event_name not in _default_allowlist(), (
        f"{event_name!r} is healthy progress and must stay out of the default"
    )


# ---------------------------------------------------------------------------
# Spooling behaviour with the default allowlist (env var UNSET)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_name", REQUIRED_DEFAULT_EVENTS)
def test_default_allowlist_spools_each_required_event(
    outbox_env, monkeypatch, event_name
):
    """Enabled + no explicit allowlist -> each default event is spooled."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    plan = f"plan-{event_name}"
    outbox_sink(_notif(plan, event_name, message=f"needs a human: {event_name}"))

    assert _spooled_events(outbox_env, plan) == [event_name], (
        f"{event_name!r} must be spooled under the default allowlist"
    )


def test_default_allowlist_rejects_healthy_progress_event(outbox_env, monkeypatch):
    """``story_merged`` is not in the default allowlist -> nothing written."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    outbox_sink(_notif("p-merged", HEALTHY_PROGRESS_EVENT, message="merged story"))
    assert not _outbox_path(outbox_env, "p-merged").exists()

    # Control: the same settings must spool an allowlisted event, proving the
    # rejection above was allowlist-driven and not a dead sink.
    outbox_sink(_notif("p-merged", "plan_completed", message="control"))
    assert _spooled_events(outbox_env, "p-merged") == ["plan_completed"]


def test_default_allowlist_rejects_missing_none_and_absent_payload(
    outbox_env, monkeypatch
):
    """Malformed notifications (no structured event name) are never spooled."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    plan = "p-malformed"

    # Missing ``event`` key inside a present payload.
    outbox_sink(make_event("notification", plan, payload={"message": "no event key"}))
    # Explicit ``None`` event.
    outbox_sink(
        make_event("notification", plan, payload={"message": "none", "event": None})
    )
    # No payload at all.
    outbox_sink(
        {
            "type": "notification",
            "ts": "2024-01-01T00:00:00+00:00",
            "plan": plan,
        }
    )
    # Empty-string event is equally not an allowlisted name.
    outbox_sink(
        make_event("notification", plan, payload={"message": "empty", "event": ""})
    )

    assert _spooled_events(outbox_env, plan) == [], (
        "malformed notifications must not be spooled"
    )

    # Control: an allowlisted event with the same settings IS spooled.
    outbox_sink(_notif(plan, "story_parked", message="control"))
    assert _spooled_events(outbox_env, plan) == ["story_parked"]


# ---------------------------------------------------------------------------
# Explicit allowlist overrides the default entirely
# ---------------------------------------------------------------------------

def test_explicit_events_env_overrides_default_entirely(outbox_env, monkeypatch):
    """``PIPELINE_NOTIFY_OUTBOX_EVENTS`` replaces, never extends, the default."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(OUTBOX_EVENTS_ENV, "a,b")
    plan = "p-override"

    # A default event is now rejected...
    outbox_sink(_notif(plan, "plan_completed", message="default event"))
    # ...and so is another default event.
    outbox_sink(_notif(plan, "story_parked", message="another default event"))
    assert _spooled_events(outbox_env, plan) == [], (
        "an explicit allowlist must reject events outside it"
    )

    # Only the explicitly listed names pass.
    outbox_sink(_notif(plan, "a", message="explicit a"))
    outbox_sink(_notif(plan, "b", message="explicit b"))
    assert _spooled_events(outbox_env, plan) == ["a", "b"]


def test_explicit_events_env_whitespace_is_tolerated(outbox_env, monkeypatch):
    """Whitespace around explicit names must not defeat the override."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(OUTBOX_EVENTS_ENV, " a , b ")
    plan = "p-override-ws"

    outbox_sink(_notif(plan, "plan_completed", message="default event"))
    outbox_sink(_notif(plan, "a", message="explicit a"))
    assert _spooled_events(outbox_env, plan) == ["a"]


# ---------------------------------------------------------------------------
# Disabled sink spools nothing, whatever the allowlist says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_name", REQUIRED_DEFAULT_EVENTS)
def test_disabled_sink_spools_none_of_the_defaults(
    outbox_env, monkeypatch, event_name
):
    """With the outbox disabled, no default event is spooled."""
    monkeypatch.delenv(OUTBOX_ENABLED_ENV, raising=False)
    plan = f"disabled-{event_name}"
    outbox_sink(_notif(plan, event_name, message="should not be spooled"))
    assert not _outbox_path(outbox_env, plan).exists()


def test_disabled_sink_spools_nothing_even_with_explicit_allowlist(
    outbox_env, monkeypatch
):
    """``"0"`` is not ``"1"``: the enabled gate wins over any allowlist."""
    monkeypatch.setenv(OUTBOX_ENABLED_ENV, "0")
    monkeypatch.setenv(OUTBOX_EVENTS_ENV, "story_parked")
    outbox_sink(_notif("p-disabled", "story_parked", message="should not be spooled"))
    assert not _outbox_path(outbox_env, "p-disabled").exists()


# ---------------------------------------------------------------------------
# Documentation must match the code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_name", REQUIRED_DEFAULT_EVENTS)
def test_module_docstring_documents_each_new_default_event(event_name):
    """The selection-rules bullet must list the events the code defaults to."""
    docstring = notification_outbox.__doc__ or ""
    assert event_name in docstring, (
        f"module docstring must document {event_name!r} as a default event"
    )


def test_module_docstring_no_longer_claims_plan_completed_is_the_only_default():
    """The stale ``(default ``"plan_completed"``)`` bullet must be updated."""
    docstring = notification_outbox.__doc__ or ""
    assert '(default ``"plan_completed"``)' not in docstring, (
        "the docstring still claims the default allowlist is only plan_completed"
    )


# ---------------------------------------------------------------------------
# The one authorized existing-test docstring edit
# ---------------------------------------------------------------------------

def test_sink_test_docstring_updated_for_the_widened_default():
    """The sink test's docstring must describe the new default, not the old.

    This is the single AUTHORIZED existing-test edit for this story: the
    docstring documents the default value, which this story changes.  The
    test's assertions are untouched and must stay untouched.
    """
    sink_test = (
        Path(__file__).resolve().parent / "test_notification_outbox_sink.py"
    )
    source = sink_test.read_text(encoding="utf-8")

    assert "The default allowlist is ``plan_completed`` plus the parked/failed story" in source, (
        "the sink test docstring must be updated to describe the widened default"
    )
    assert "The default allowlist is ``plan_completed``; e.g. every dispatch-retry" not in source, (
        "the stale sink test docstring line must be replaced"
    )
    # The assertions of that test must remain untouched: it still uses
    # ``story_merged``, which stays outside the new default.
    assert '"event": "story_merged"' in source
