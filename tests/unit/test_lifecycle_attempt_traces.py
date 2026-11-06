"""Contract for one trace per dispatch attempt in the lifecycle sink.

A story runs for hours or days, so a single trace per story is the wrong shape:
the unit an engineer debugs is one dispatch attempt. The telemetry sink groups
every lifecycle span of the same attempt under one deterministic trace id
derived from the event's ``correlation_id`` and the attempt number the
``agent_dispatched`` event carries, and links an attempt-1+ span back to the
previous attempt's root.

Everything runs against a real ``TracerProvider`` with an in-memory span
exporter -- no OTLP endpoint is contacted. ``telemetry.configure`` installs a
``SimpleSpanProcessor`` for an injected exporter, so a span is readable from the
exporter as soon as it ends.

The import of ``pipeline.server`` comes first: a pipeline module imported cold
without it hits the pre-existing import cycle (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from pipeline import telemetry
from pipeline.trace_context import ATTEMPT_ROOT_SPAN_ID, attempt_trace_id

pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

#: 12-hex per-story id the dispatcher stamps on every lifecycle event.
CID = "c0ffee1234ab"
PLAN = "obs-trace-context"
STORY = "e9073a8b-4d90-4686-810d-69ef470c6ab4"

ROOT_SPAN_ID = int(ATTEMPT_ROOT_SPAN_ID, 16)

ENDPOINT_ENVS = (
    "PIPELINE_OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


@pytest.fixture
def exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    memory = InMemorySpanExporter()
    telemetry.configure(memory)
    return memory


def _dispatch(attempt, *, plan=PLAN, story=STORY, correlation_id=CID):
    """Publish the one event that carries an attempt number."""
    telemetry.record_lifecycle_event(
        {
            "type": "agent_dispatched",
            "plan": plan,
            "story_key": story,
            "correlation_id": correlation_id,
            "payload": {"attempt": attempt, "outcome": "dispatched"},
        }
    )


def _event(*, plan=PLAN, story=STORY, correlation_id=CID, event_type="agent_finished"):
    """Publish a lifecycle event that carries no attempt number."""
    telemetry.record_lifecycle_event(
        {
            "type": event_type,
            "plan": plan,
            "story_key": story,
            "correlation_id": correlation_id,
            "payload": {
                "outcome": "merged",
                "backend": "claude",
                "provider": "anthropic",
            },
        }
    )


def _trace_id(correlation_id, attempt):
    return int(attempt_trace_id(correlation_id, attempt), 16)


def _spans(exporter):
    return exporter.get_finished_spans()


def _only_span(exporter):
    """The one span exported since the last ``clear()``."""
    spans = _spans(exporter)
    assert len(spans) == 1
    return spans[0]


def test_events_of_one_attempt_share_a_trace_and_the_fixed_root_parent(exporter):
    _dispatch(0)
    exporter.clear()
    _event()
    _event()

    spans = _spans(exporter)
    assert len(spans) == 2
    expected = _trace_id(CID, 0)
    assert spans[0].context.trace_id == expected
    assert spans[1].context.trace_id == expected
    assert spans[0].context.trace_id == spans[1].context.trace_id
    assert spans[0].parent.trace_id == expected
    assert spans[1].parent.trace_id == expected
    assert spans[0].parent.span_id == ROOT_SPAN_ID
    assert spans[0].parent.is_remote is True


def test_trace_id_is_deterministic_across_reset(exporter):
    _dispatch(0)
    exporter.clear()
    _event()
    first = _spans(exporter)[0].context.trace_id

    telemetry.reset()
    # ``reset()`` shuts the provider down, and a shut-down in-memory exporter
    # silently drops everything it is handed, so the fresh state gets a fresh
    # exporter rather than reviving the old one.
    fresh = type(exporter)()
    telemetry.configure(fresh)
    _dispatch(0)
    fresh.clear()
    _event()

    assert _spans(fresh)[0].context.trace_id == first
    assert first == _trace_id(CID, 0)


def test_new_attempt_opens_a_new_trace(exporter):
    _dispatch(0)
    exporter.clear()
    _event()
    first = _spans(exporter)[0].context.trace_id
    exporter.clear()

    _dispatch(1)
    exporter.clear()
    _event()
    second = _spans(exporter)[0].context.trace_id

    assert first == _trace_id(CID, 0)
    assert second == _trace_id(CID, 1)
    assert first != second


def test_event_without_correlation_id_is_a_root_span(exporter):
    _event(correlation_id=None)

    spans = _spans(exporter)
    assert len(spans) == 1
    assert spans[0].parent is None


def test_malformed_correlation_id_is_a_root_span(exporter):
    _event(correlation_id="abc def")

    spans = _spans(exporter)
    assert len(spans) == 1
    assert spans[0].parent is None


def test_attributes_are_the_unchanged_lifecycle_set(exporter):
    _event()

    spans = _spans(exporter)
    assert len(spans) == 1
    assert set(spans[0].attributes) == {
        "fagan.plan",
        "fagan.story_key",
        "fagan.event_type",
        "fagan.outcome",
        "fagan.backend",
        "fagan.provider",
        "correlation_id",
    }


def test_invalid_attempt_values_do_not_replace_a_recorded_attempt(exporter):
    _dispatch(1)
    _dispatch(-1)
    _dispatch("x")
    exporter.clear()

    _event()

    assert _only_span(exporter).context.trace_id == _trace_id(CID, 1)


def test_attempt_map_is_bounded_and_evicts_the_oldest(exporter):
    for index in range(1, 514):
        _dispatch(0, story=f"S{index}")

    assert len(telemetry._ATTEMPTS) == 512
    assert (PLAN, "S1") not in telemetry._ATTEMPTS
    assert (PLAN, "S513") in telemetry._ATTEMPTS


def test_redispatch_refreshes_a_story_against_eviction(exporter):
    for index in range(1, 514):
        _dispatch(0, story=f"S{index}")
    # 512 entries: S1 was evicted on the way in and S2 is now the quietest.
    _dispatch(0, story="S1")

    assert len(telemetry._ATTEMPTS) == 512
    assert (PLAN, "S1") in telemetry._ATTEMPTS
    assert (PLAN, "S2") not in telemetry._ATTEMPTS


def test_first_attempt_span_carries_no_link(exporter):
    _dispatch(0)
    exporter.clear()

    _event()

    assert list(_only_span(exporter).links) == []


def test_second_attempt_links_to_the_first_attempt_root(exporter):
    _dispatch(0)
    _dispatch(1)
    exporter.clear()

    _event()

    links = list(_only_span(exporter).links)
    assert len(links) == 1
    assert links[0].context.trace_id == _trace_id(CID, 0)
    assert links[0].context.span_id == ROOT_SPAN_ID


def test_third_attempt_links_only_to_the_second_attempt_root(exporter):
    _dispatch(0)
    _dispatch(1)
    _dispatch(2)
    exporter.clear()

    _event()
    first_seen = _only_span(exporter)
    exporter.clear()
    _event()
    second_seen = _only_span(exporter)

    for span in (first_seen, second_seen):
        assert span.context.trace_id == _trace_id(CID, 2)
        links = list(span.links)
        assert len(links) == 1
        assert links[0].context.trace_id == _trace_id(CID, 1)
        assert links[0].context.trace_id != _trace_id(CID, 0)
        assert links[0].context.span_id == ROOT_SPAN_ID
