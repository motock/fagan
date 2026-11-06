"""TDD tests for honouring the standard OpenTelemetry exporter env vars.

``pipeline/telemetry`` used to switch on only from the deprecated
``PIPELINE_OTEL_ENDPOINT`` alias. A deployment that configures its collector
the standard way -- ``OTEL_EXPORTER_OTLP_ENDPOINT`` or the signal-specific
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``
-- must enable the matching signal. A set standard variable wins over the alias
for that signal, because the OTLP exporter reads the standard variables
(headers, certificate, timeout) itself and an endpoint passed explicitly would
pair them with the wrong collector.

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
    yield
    telemetry.reset()


def test_enabled_with_generic_standard_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")

    assert telemetry.enabled() is True


def test_enabled_with_traces_specific_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4317")

    assert telemetry.enabled() is True


def test_alias_still_enables_tracing_and_metrics(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")

    assert telemetry.enabled() is True
    assert telemetry.configure_metrics() is True


def test_nothing_set_disables_both_signals():
    assert telemetry.enabled() is False
    assert telemetry.configure_metrics() is False


def test_metrics_only_standard_endpoint_enables_metrics_not_tracing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://metrics:4317")

    assert telemetry.enabled() is False
    assert telemetry.configure_metrics() is True


def test_standard_variable_wins_over_alias_for_traces(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://std:4317")
    monkeypatch.setenv(telemetry.ENDPOINT_ENV, "http://alias:4317")

    assert telemetry._signal_endpoint(telemetry._TRACES_ENDPOINT_ENVS) == (True, None)


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
