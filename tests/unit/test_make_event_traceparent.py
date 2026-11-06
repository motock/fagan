"""Tests for the optional ``traceparent`` key on ``make_event``.

``make_event`` stamps the active W3C trace-context value as a TOP-LEVEL
``"traceparent"`` key so existing consumers can correlate an event with the
span that produced it, without any schema change. When no span is active the
key is absent entirely and the event shape is unchanged.
"""

import json

import pytest

import pipeline.events
import pipeline.server
import pipeline.trace_context

SPEC = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
ACTIVE = "00-11111111111111111111111111111111-2222222222222222-01"


def test_make_event_stamps_active_traceparent(monkeypatch):
    monkeypatch.setattr(
        pipeline.trace_context, "current_traceparent", lambda: SPEC
    )
    event = pipeline.events.make_event("story_ready", "plan-1")
    assert event["traceparent"] == SPEC


def test_explicit_traceparent_wins_over_active_span(monkeypatch):
    monkeypatch.setattr(
        pipeline.trace_context, "current_traceparent", lambda: ACTIVE
    )
    event = pipeline.events.make_event("story_ready", "plan-1", traceparent=SPEC)
    assert event["traceparent"] == SPEC
    assert event["traceparent"] != ACTIVE


def test_no_key_when_no_span_active(monkeypatch):
    monkeypatch.setattr(
        pipeline.trace_context, "current_traceparent", lambda: None
    )
    event = pipeline.events.make_event("story_ready", "plan-1")
    assert "traceparent" not in event


def test_malformed_explicit_traceparent_raises():
    with pytest.raises(
        ValueError, match=r"^traceparent must be a W3C traceparent$"
    ):
        pipeline.events.make_event(
            "story_ready", "plan-1", traceparent="not-a-traceparent"
        )


def test_non_str_explicit_traceparent_raises():
    with pytest.raises(
        ValueError, match=r"^traceparent must be a W3C traceparent$"
    ):
        pipeline.events.make_event("story_ready", "plan-1", traceparent=123)


def test_stamped_traceparent_survives_json_round_trip(monkeypatch):
    monkeypatch.setattr(
        pipeline.trace_context, "current_traceparent", lambda: SPEC
    )
    event = pipeline.events.make_event("story_ready", "plan-1")
    assert json.loads(json.dumps(event))["traceparent"] == SPEC
