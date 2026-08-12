"""Tests for pipeline.events - event construction and in-process event bus.

These tests are written FIRST (TDD). The implementation module
``pipeline.events`` does not exist yet, so this suite is expected to be RED
until a later dispatch implements it.
"""

import logging
from datetime import datetime

import pytest

from pipeline import events

# ---------------------------------------------------------------------------
# Module-level constant EVENT_TYPES
# ---------------------------------------------------------------------------

EXPECTED_EVENT_TYPES = frozenset(
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


def test_event_types_is_frozenset():
    assert isinstance(events.EVENT_TYPES, frozenset)


def test_event_types_contains_exactly_expected_members():
    assert events.EVENT_TYPES == EXPECTED_EVENT_TYPES


# ---------------------------------------------------------------------------
# make_event
# ---------------------------------------------------------------------------


def test_make_event_returns_all_five_keys():
    ev = events.make_event("story_ready", "plan-1")
    assert set(ev.keys()) == {"type", "plan", "story_key", "payload", "ts"}


def test_make_event_values_populated():
    ev = events.make_event(
        "agent_done", "plan-1", story_key="S-3", payload={"x": 1}
    )
    assert ev["type"] == "agent_done"
    assert ev["plan"] == "plan-1"
    assert ev["story_key"] == "S-3"
    assert ev["payload"] == {"x": 1}


def test_make_event_ts_is_iso8601():
    ev = events.make_event("story_ready", "plan-1")
    # Must parse as ISO 8601 (datetime.fromisoformat handles the UTC form).
    parsed = datetime.fromisoformat(ev["ts"])
    assert parsed is not None
    # Should be timezone-aware (UTC).
    assert parsed.tzinfo is not None


def test_make_event_unknown_type_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("not_a_real_type", "plan-1")


def test_make_event_unknown_type_message_mentions_type():
    with pytest.raises(ValueError) as excinfo:
        events.make_event("not_a_real_type", "plan-1")
    assert "not_a_real_type" in str(excinfo.value)


def test_make_event_empty_plan_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("story_ready", "")


def test_make_event_none_plan_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("story_ready", None)


def test_make_event_defaults_payload_to_empty_dict():
    ev = events.make_event("story_ready", "plan-1")
    assert ev["payload"] == {}


def test_make_event_defaults_story_key_to_none():
    ev = events.make_event("story_ready", "plan-1")
    assert ev["story_key"] is None


def test_make_event_payload_none_defaults_to_empty_dict():
    ev = events.make_event("story_ready", "plan-1", payload=None)
    assert ev["payload"] == {}


def test_make_event_every_valid_type_accepted():
    for t in EXPECTED_EVENT_TYPES:
        ev = events.make_event(t, "plan-1")
        assert ev["type"] == t


# ---------------------------------------------------------------------------
# EventBus abstract base class
# ---------------------------------------------------------------------------


def test_eventbus_is_abstract():
    assert issubclass(events.InProcessEventBus, events.EventBus)
    # Cannot instantiate the abstract base directly.
    with pytest.raises(TypeError):
        events.EventBus()  # type: ignore[abstract]


def test_eventbus_has_publish_and_subscribe_abstract_methods():
    # The abstract methods must be declared on the base class.
    assert "publish" in events.EventBus.__abstractmethods__
    assert "subscribe" in events.EventBus.__abstractmethods__


# ---------------------------------------------------------------------------
# InProcessEventBus
# ---------------------------------------------------------------------------


def test_subscribe_and_publish_calls_handler_once():
    bus = events.InProcessEventBus()
    calls = []
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert calls == [ev]


def test_two_handlers_same_type_both_called():
    bus = events.InProcessEventBus()
    a = []
    b = []
    bus.subscribe("story_ready", a.append)
    bus.subscribe("story_ready", b.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert a == [ev]
    assert b == [ev]


def test_publish_no_subscribers_does_not_raise():
    bus = events.InProcessEventBus()
    ev = events.make_event("story_ready", "plan-1")
    # No subscribers registered for this type - silent no-op.
    bus.publish(ev)  # must not raise


def test_handler_that_raises_does_not_stop_others(caplog):
    bus = events.InProcessEventBus()

    second_called = []

    def bad_handler(_event):
        raise RuntimeError("boom")

    def good_handler(event):
        second_called.append(event)

    bus.subscribe("story_ready", bad_handler)
    bus.subscribe("story_ready", good_handler)

    ev = events.make_event("story_ready", "plan-1")
    with caplog.at_level(logging.ERROR, logger="pipeline.events"):
        # Must not propagate the bad handler's exception.
        bus.publish(ev)

    assert second_called == [ev]
    # The exception should have been logged at ERROR level.
    assert any(
        record.levelno == logging.ERROR and "boom" in record.getMessage()
        for record in caplog.records
    )


def test_publish_only_calls_handlers_for_matching_type():
    bus = events.InProcessEventBus()
    ready_calls = []
    done_calls = []
    bus.subscribe("story_ready", ready_calls.append)
    bus.subscribe("agent_done", done_calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert ready_calls == [ev]
    assert done_calls == []


def test_subscribe_appends_does_not_replace():
    bus = events.InProcessEventBus()
    calls = []
    bus.subscribe("story_ready", calls.append)
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    # Two subscriptions => handler called twice.
    assert calls == [ev, ev]