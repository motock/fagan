"""TDD tests for the opt-in OpenTelemetry tracing seam (``pipeline/telemetry``).

Tracing is OFF unless ``PIPELINE_OTEL_ENDPOINT`` is set: an unset endpoint must
cost nothing, not even an ``opentelemetry`` import. When it is set, dashboard
``/api/`` requests and pipeline lifecycle events (published on the event bus)
each produce exactly one span, and the span carries only non-sensitive
attributes: method, route template, status, correlation id, plan, story key,
event type, outcome and backend/provider name. Never a query string, header,
prompt, brief or model output.

An exporter that raises must never fail the request or the tick; it is logged
once per minute at WARNING with the error *type* only.

``pipeline.server`` is imported before any other ``pipeline.*`` module: the
package has a pre-existing import cycle that this suite inherits (same idiom
as ``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import sys

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from pipeline import telemetry

pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")


def _memory_exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


class _RaisingExporter:
    """An exporter whose every ``export`` call raises."""

    def export(self, spans):
        raise RuntimeError("exporter down: secret-token-in-message")

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis=30000):
        return True


def test_unset_endpoint_is_a_noop_and_imports_no_otel(monkeypatch):
    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    telemetry.reset()
    for name in [m for m in sys.modules if m.startswith("opentelemetry")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    assert telemetry.enabled() is False

    # Exercising the seam must not drag the SDK in, and must not raise.
    with telemetry.span("dashboard.request", {"http.request.method": "GET"}):
        pass
    telemetry.record_lifecycle_event(
        {"type": "agent_dispatched", "plan": "p", "story_key": "S-1", "payload": {}}
    )

    assert [m for m in sys.modules if m.startswith("opentelemetry")] == []


def test_endpoint_configured_is_enabled(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces")

    assert telemetry.enabled() is True


def test_request_yields_one_span_with_allowed_attributes_only():
    """A real dashboard request traced through the app's own middleware."""
    from fastapi.testclient import TestClient

    from app import dashboard as d
    from app.auth import get_or_create_api_key

    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)
    client = TestClient(d.app)

    response = client.get(
        "/api/health?plan=secret-plan",
        headers={
            "X-Pipeline-Api-Key": get_or_create_api_key(),
            "X-Correlation-ID": "corr-1",
        },
    )
    assert response.status_code == 200

    spans = [s for s in exporter.get_finished_spans() if s.name == telemetry.REQUEST_SPAN_NAME]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.route"] == "/api/health"
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["correlation_id"] == "corr-1"
    assert set(span.attributes) == {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "correlation_id",
    }
    # The query string, any header value or a body never reaches a span.
    values = {str(v) for v in span.attributes.values()}
    assert "secret-plan" not in " ".join(values)
    assert "?" not in " ".join(v for v in values if v.startswith("/"))


def test_refused_api_request_is_traced_with_its_status():
    from fastapi.testclient import TestClient

    from app import dashboard as d

    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)
    client = TestClient(d.app)
    client.headers.pop("X-Pipeline-Api-Key", None)

    response = client.get("/api/health", headers={"X-Pipeline-Api-Key": "wrong"})
    assert response.status_code == 401

    spans = [s for s in exporter.get_finished_spans() if s.name == telemetry.REQUEST_SPAN_NAME]
    assert len(spans) == 1
    assert spans[0].attributes["http.response.status_code"] == 401
    assert "http.route" not in spans[0].attributes


def test_non_api_request_is_not_traced():
    from fastapi.testclient import TestClient

    from app import dashboard as d

    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)
    client = TestClient(d.app)

    client.get("/")

    assert exporter.get_finished_spans() == ()


def test_lifecycle_event_yields_one_span_with_its_outcome():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    telemetry.record_lifecycle_event(
        {
            "type": "agent_dispatched",
            "plan": "demo",
            "story_key": "OT-1",
            "correlation_id": "corr-2",
            "payload": {
                "outcome": "merged",
                "backend": "claude",
                "prompt": "SECRET PROMPT TEXT",
            },
        }
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == telemetry.LIFECYCLE_SPAN_NAME
    assert span.attributes["pipeline.plan"] == "demo"
    assert span.attributes["pipeline.story_key"] == "OT-1"
    assert span.attributes["pipeline.event_type"] == "agent_dispatched"
    assert span.attributes["pipeline.outcome"] == "merged"
    assert span.attributes["pipeline.backend"] == "claude"
    assert span.attributes["correlation_id"] == "corr-2"
    assert "prompt" not in {k.split(".")[-1] for k in span.attributes}
    assert "SECRET PROMPT TEXT" not in {str(v) for v in span.attributes.values()}


def test_raising_exporter_does_not_fail_the_caller(caplog):
    telemetry.configure(exporter=_RaisingExporter())

    with caplog.at_level("WARNING", logger="pipeline.telemetry"):
        with telemetry.span(telemetry.REQUEST_SPAN_NAME, {"http.request.method": "GET"}):
            pass
        telemetry.record_lifecycle_event(
            {"type": "parked", "plan": "demo", "story_key": "OT-1", "payload": {}}
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("export" in m.lower() or "trace" in m.lower() for m in messages)
    # The warning names the error TYPE, never its message (which may hold secrets).
    assert not any("secret-token-in-message" in m for m in messages)


def test_repeated_exporter_failures_log_at_most_once_per_minute(caplog, monkeypatch):
    telemetry.configure(exporter=_RaisingExporter())
    now = [1000.0]
    monkeypatch.setattr(telemetry, "_monotonic", lambda: now[0])

    with caplog.at_level("WARNING", logger="pipeline.telemetry"):
        for _ in range(5):
            with telemetry.span(telemetry.REQUEST_SPAN_NAME, {}):
                pass
        now[0] += 30.0
        with telemetry.span(telemetry.REQUEST_SPAN_NAME, {}):
            pass

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    now[0] += 61.0
    with caplog.at_level("WARNING", logger="pipeline.telemetry"), telemetry.span(
        telemetry.REQUEST_SPAN_NAME, {}
    ):
        pass

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


def test_lifecycle_sink_is_registered_on_the_bus_only_when_enabled(monkeypatch):
    from pipeline.event_wiring import build_bus

    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    bus_off = build_bus()
    assert telemetry.record_lifecycle_event not in bus_off._handlers.get(
        "agent_dispatched", []
    )

    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces")
    bus_on = build_bus()
    assert telemetry.record_lifecycle_event in bus_on._handlers.get(
        "agent_dispatched", []
    )
    assert telemetry.record_lifecycle_event in bus_on._handlers.get("parked", [])


def test_published_lifecycle_event_produces_a_span():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    from pipeline.event_wiring import build_bus

    bus = build_bus()
    bus.publish(
        {
            "type": "agent_dispatched",
            "plan": "demo",
            "story_key": "OT-2",
            "payload": {"outcome": "dispatched", "provider": "ollama"},
        }
    )

    lifecycle = [s for s in exporter.get_finished_spans() if s.name == telemetry.LIFECYCLE_SPAN_NAME]
    assert len(lifecycle) == 1
    span = lifecycle[0]
    assert span.attributes["pipeline.story_key"] == "OT-2"
    assert span.attributes["pipeline.outcome"] == "dispatched"
    assert span.attributes["pipeline.provider"] == "ollama"


def test_malformed_event_never_raises_and_emits_nothing():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    telemetry.record_lifecycle_event({"type": "parked"})

    assert exporter.get_finished_spans() == ()
