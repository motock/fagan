"""TDD tests for honouring the standard OpenTelemetry exporter env vars.

``pipeline/telemetry`` used to switch on only from the deprecated
``PIPELINE_OTEL_ENDPOINT`` alias. A deployment that configures its collector
the standard way -- ``OTEL_EXPORTER_OTLP_ENDPOINT`` or the signal-specific
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``
-- must enable the matching signal. A set standard variable wins over the alias
for that signal, because the OTLP exporter reads the standard variables
(headers, certificate, timeout) itself and an endpoint passed explicitly would
pair them with the wrong collector.

No test here contacts a collector: the metrics are exercised through an
in-memory reader and the span exporter is replaced by a recorder, so the
switch and the exporter construction are both pinned without a background
export thread.

``pipeline.server`` is imported before any other ``pipeline.*`` module: the
package has a pre-existing import cycle this suite inherits (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from pipeline import telemetry

#: The standard variables this module might otherwise inherit from a developer's
#: or CI shell. Each test starts from a clean slate.
_STANDARD_ENVS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    for name in (*_STANDARD_ENVS, telemetry.ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    telemetry.reset()
    telemetry.reset_metrics()
    yield
    telemetry.reset()
    telemetry.reset_metrics()


def _in_memory_metrics_provider():
    """A ``MeterProvider`` writing to memory plus its reader; never exports."""
    pytest.importorskip("opentelemetry.sdk.metrics.export")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def _record_span_exporter(monkeypatch) -> list[dict]:
    """Replace the OTLP span exporter with a recorder; return its call kwargs.

    ``configure`` imports ``OTLPSpanExporter`` at call time, so patching the
    module attribute intercepts the construction: the kwargs it records are
    exactly what the implementation passed, with no collector ever contacted.
    """
    module = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    calls: list[dict] = []

    class _RecordingExporter:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def export(self, spans) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(module, "OTLPSpanExporter", _RecordingExporter)
    return calls


def test_enabled_with_generic_standard_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")

    assert telemetry.enabled() is True


def test_enabled_with_traces_specific_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4317")

    assert telemetry.enabled() is True


def test_alias_still_enables_tracing_and_metrics(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")
    provider, _reader = _in_memory_metrics_provider()

    try:
        assert telemetry.enabled() is True
        assert telemetry.configure_metrics(provider) is True
    finally:
        provider.shutdown()


def test_nothing_set_disables_both_signals():
    assert telemetry.enabled() is False
    assert telemetry.configure_metrics() is False


def test_metrics_only_standard_endpoint_enables_metrics_not_tracing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://metrics:4317")
    provider, _reader = _in_memory_metrics_provider()

    try:
        assert telemetry.enabled() is False
        assert telemetry.configure_metrics(provider) is True
    finally:
        provider.shutdown()


def test_standard_variable_wins_over_alias_for_traces(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://std:4317")
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")

    assert telemetry._signal_endpoint(telemetry._TRACES_ENDPOINT_ENVS) == (True, None)


def test_configure_builds_a_tracer_with_no_explicit_endpoint(monkeypatch):
    """A standard variable must yield ``OTLPSpanExporter()``, not an alias URL.

    An explicit ``endpoint`` kwarg would override the standard variable and
    desynchronise ``OTEL_EXPORTER_OTLP_HEADERS``/``_CERTIFICATE``/``_TIMEOUT``.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://std:4317")
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")
    calls = _record_span_exporter(monkeypatch)

    assert telemetry._tracer() is not None
    assert calls == [{}]


def test_configure_passes_the_alias_url_as_the_endpoint(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")
    calls = _record_span_exporter(monkeypatch)

    assert telemetry._tracer() is not None
    assert calls == [{"endpoint": "http://alias:4317"}]


def test_unconfigured_tracer_does_not_stick(monkeypatch):
    """An unconfigured first call must not memoise a no-op tracer."""
    assert telemetry._tracer() is None
    assert telemetry._TRACER is None

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4317")
    _record_span_exporter(monkeypatch)

    assert telemetry._tracer() is not None


def test_empty_standard_endpoint_counts_as_unset(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")

    assert telemetry._signal_endpoint(telemetry._TRACES_ENDPOINT_ENVS) == (
        True,
        "http://alias:4317",
    )


def test_empty_standard_endpoint_alone_does_not_enable_tracing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    assert telemetry._signal_endpoint(telemetry._TRACES_ENDPOINT_ENVS) == (False, None)
    assert telemetry.enabled() is False
    assert telemetry.configure_metrics() is False
