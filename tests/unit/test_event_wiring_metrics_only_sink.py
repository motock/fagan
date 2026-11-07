"""The lifecycle sink must be subscribed for a metrics-only deployment.

A deployment can export metrics without exporting traces
(``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`` set, no traces endpoint).  Trace
telemetry is opt-in through :func:`pipeline.telemetry.enabled`, but
:func:`pipeline.telemetry.record_lifecycle_event` also feeds the story-duration
histograms, so the sink has to be subscribed when *metrics* export is on even
though no tracer exists.  Without a tracer the sink returns right after
recording the duration, so the subscription costs one dict lookup per event and
emits no span.
"""

from __future__ import annotations

import pytest

from pipeline import event_wiring, telemetry

GENERIC_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
METRICS_ENDPOINT = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

_ENDPOINT_VARS = (GENERIC_ENDPOINT, TRACES_ENDPOINT, METRICS_ENDPOINT)


def _reset_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every endpoint variable and drop the module-level exporter state.

    ``_EXPORTER`` is module state, so a leftover from another test would make
    ``enabled()`` true regardless of the environment under test.
    """
    for name in _ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    telemetry.reset()
    telemetry.reset_metrics()
    monkeypatch.setattr(telemetry, "_EXPORTER", None, raising=False)
    monkeypatch.setattr(telemetry, "_TRACER", None, raising=False)
    monkeypatch.setattr(telemetry, "_METER", None, raising=False)
    monkeypatch.setattr(telemetry, "_METER_PROVIDER", None, raising=False)
    assert telemetry._EXPORTER is None


def _handlers(bus, event_type: str) -> list:
    """Return the handlers subscribed to *event_type* on *bus*."""
    return list(bus._handlers.get(event_type, []))


def _has_lifecycle_sink(bus, event_type: str) -> bool:
    return telemetry.record_lifecycle_event in _handlers(bus, event_type)


def _pr_open_event() -> dict:
    return {
        "type": "pr_open",
        "plan": "p1",
        "story_key": "S1",
        "payload": {"outcome": "ok"},
    }


def test_should_subscribe_the_lifecycle_sink_when_only_a_metrics_endpoint_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics-only: every lifecycle event type carries the recording sink."""
    _reset_telemetry(monkeypatch)
    monkeypatch.setenv(METRICS_ENDPOINT, "http://collector:4318/v1/metrics")
    assert telemetry.enabled() is False

    bus = event_wiring.build_bus()

    for event_type in telemetry.LIFECYCLE_EVENT_TYPES:
        assert _has_lifecycle_sink(bus, event_type), (
            f"missing record_lifecycle_event for {event_type!r} "
            f"(handlers={_handlers(bus, event_type)!r})"
        )


def test_should_not_subscribe_the_lifecycle_sink_when_no_endpoint_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No endpoint at all: the opt-in sink is absent, so a tick pays nothing."""
    _reset_telemetry(monkeypatch)
    assert telemetry.enabled() is False
    assert telemetry.metrics_enabled() is False

    bus = event_wiring.build_bus()

    for event_type in telemetry.LIFECYCLE_EVENT_TYPES:
        # ``agent_done`` is a lifecycle type that always carries wake_handler,
        # so the absence assertion is about the recording sink specifically.
        assert not _has_lifecycle_sink(bus, event_type), (
            f"unexpected record_lifecycle_event for {event_type!r}: "
            f"{_handlers(bus, event_type)!r}"
        )


def test_should_report_metrics_enabled_only_when_a_metrics_endpoint_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``metrics_enabled()`` follows the metrics signal, not the traces one."""
    _reset_telemetry(monkeypatch)
    assert telemetry.metrics_enabled() is False

    # A traces-only deployment must not claim metrics export.
    monkeypatch.setenv(TRACES_ENDPOINT, "http://collector:4318/v1/traces")
    assert telemetry.metrics_enabled() is False
    monkeypatch.delenv(TRACES_ENDPOINT, raising=False)

    monkeypatch.setenv(METRICS_ENDPOINT, "http://collector:4318/v1/metrics")
    assert telemetry.metrics_enabled() is True
    monkeypatch.delenv(METRICS_ENDPOINT, raising=False)

    # The generic variable configures every signal, metrics included.
    monkeypatch.setenv(GENERIC_ENDPOINT, "http://collector:4318")
    assert telemetry.metrics_enabled() is True


def test_should_not_start_a_span_for_a_metrics_only_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing a lifecycle event with metrics only records; it never traces."""
    _reset_telemetry(monkeypatch)
    monkeypatch.setenv(METRICS_ENDPOINT, "http://collector:4318/v1/metrics")
    assert telemetry._tracer() is None

    bus = event_wiring.build_bus()
    assert _has_lifecycle_sink(bus, "pr_open")

    bus.publish(_pr_open_event())

    assert telemetry._tracer() is None
