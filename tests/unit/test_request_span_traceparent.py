"""TDD contract for continuing an inbound ``traceparent`` on the request span.

A caller that already has a trace -- the dashboard's own front end, or an
upstream gateway -- sends the W3C ``traceparent`` header and expects its trace
to continue through Fagan instead of starting a second, unrelated one. The
header is caller-controlled, so it is honoured only when
``pipeline.trace_context.parse_traceparent`` accepts it: exactly version ``00``,
a 32-character lowercase hex trace id and a 16-character lowercase hex span id,
neither all zero, and 2 hex flags. ``tracestate`` is never propagated (it is
unbounded, caller-controlled data), and nothing here logs a header value.

Everything runs against a real ``TracerProvider`` with an in-memory span
exporter -- no OTLP endpoint is contacted. ``telemetry.configure`` installs a
``SimpleSpanProcessor`` for an injected exporter, so a span is readable from the
exporter as soon as ``_RequestSpan.finish`` ends it.

The tests are defined in this order on purpose: test 1 supplies a valid parent,
and tests 2-4 must still see a root span afterwards. An implementation that
caches the parsed context in module state, or attaches it to the ambient context
without detaching, leaks caller A's identity into every later request and turns
those tests red.

The import of ``pipeline.server`` comes first: a pipeline module imported cold
without it hits the pre-existing import cycle (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from pipeline import telemetry

pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

#: The W3C specification's own example trace context.
SPEC_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
SPEC_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
SPEC_SPAN_ID = 0xB7AD6B7169203331

CORRELATION_ID = "test-correlation-id"

ENDPOINT_ENVS = (
    "PIPELINE_OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


class _FakeRequest:
    """The parts of a Starlette request ``start_request_span`` touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.method = "GET"
        # ``_RequestSpan.finish`` reads ``scope["route"]``; no route template
        # means no ``http.route`` attribute, which no test here asserts on.
        self.scope: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


def _memory_exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


def _exported_request_span(exporter, headers: dict[str, str]):
    """Run one request through the span seam and return its exported span."""
    handle = telemetry.start_request_span(_FakeRequest(headers), CORRELATION_ID)
    handle.finish()
    spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name == telemetry.REQUEST_SPAN_NAME
        and span.attributes.get("correlation_id") == CORRELATION_ID
    ]
    if not spans:
        pytest.fail("start_request_span exported no request span for this request")
    return spans[-1]


def test_valid_traceparent_continues_the_callers_trace():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    span = _exported_request_span(exporter, {"traceparent": SPEC_TRACEPARENT})

    assert span.context.trace_id == SPEC_TRACE_ID
    assert span.parent is not None
    assert span.parent.span_id == SPEC_SPAN_ID
    assert span.parent.is_remote is True


def test_uppercase_hex_traceparent_is_rejected_and_a_new_trace_starts():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    span = _exported_request_span(
        exporter,
        {"traceparent": SPEC_TRACEPARENT.upper()},
    )

    assert span.parent is None
    assert span.context.trace_id != 0
    assert span.context.trace_id != SPEC_TRACE_ID


def test_all_zero_trace_id_is_rejected_and_a_new_trace_starts():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    span = _exported_request_span(
        exporter,
        {"traceparent": f"00-{'0' * 32}-b7ad6b7169203331-01"},
    )

    assert span.parent is None


def test_tracestate_is_never_propagated():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    span = _exported_request_span(
        exporter,
        {
            "traceparent": SPEC_TRACEPARENT,
            "tracestate": "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7",
        },
    )

    assert span.context.trace_state.to_header() == ""
    assert span.parent is not None
    assert span.parent.span_id == SPEC_SPAN_ID


def test_disabled_telemetry_returns_a_finishable_handle_and_exports_nothing():
    exporter = _memory_exporter()

    handle = telemetry.start_request_span(
        _FakeRequest({"traceparent": SPEC_TRACEPARENT}), CORRELATION_ID
    )

    assert handle.finish() is None
    assert not exporter.get_finished_spans()
