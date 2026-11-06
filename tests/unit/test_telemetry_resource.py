"""TDD tests for the mandatory OpenTelemetry resource attributes.

Every signal the pipeline exports carries three resource attributes:

* ``service.name`` -- ``fagan`` unless ``OTEL_SERVICE_NAME`` says otherwise;
* ``service.instance.id`` -- one fresh identifier per process;
* ``fagan.namespace`` -- ``default`` unless ``OTEL_RESOURCE_ATTRIBUTES`` sets it.

``OTEL_RESOURCE_ATTRIBUTES`` (comma-separated ``key=value``, values percent-decoded) may
add or override any of them; a malformed pair is ignored rather than raising. The
namespace is read *only* from the environment: no request field, plan name or repo path
can influence it, because a tenant-shaped label would otherwise be attacker-controlled.

``pipeline.server`` is imported before any other ``pipeline.*`` module: the package has a
pre-existing import cycle that this suite inherits (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

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
def _clean_environment(monkeypatch):
    """Only the environment is patched: no module state is replaced."""
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


def test_defaults_name_the_service_and_the_default_namespace():
    attributes = telemetry._resource_attributes()

    assert attributes["service.name"] == "fagan"
    assert attributes["fagan.namespace"] == "default"
    assert attributes["service.instance.id"]


def test_instance_id_is_stable_within_one_process():
    first = telemetry._resource_attributes()["service.instance.id"]
    second = telemetry._resource_attributes()["service.instance.id"]

    assert first == second


def test_resource_attributes_override_the_namespace_and_add_attributes(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "fagan.namespace=acme,deployment.environment=prod")

    attributes = telemetry._resource_attributes()

    assert attributes["fagan.namespace"] == "acme"
    assert attributes["deployment.environment"] == "prod"
    assert attributes["service.name"] == "fagan"


def test_service_name_variable_overrides_the_default_name(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "fagan-staging")

    assert telemetry._resource_attributes()["service.name"] == "fagan-staging"


@pytest.mark.parametrize("value", ["novalue", "=x", ""])
def test_malformed_resource_attribute_pairs_are_ignored(monkeypatch, value):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", value)

    attributes = telemetry._resource_attributes()

    assert attributes["service.name"] == "fagan"
    assert attributes["fagan.namespace"] == "default"
    assert attributes["service.instance.id"]
    assert "" not in attributes
    assert "novalue" not in attributes


def test_resource_attribute_values_are_percent_decoded(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "fagan.namespace=a%20b")

    assert telemetry._resource_attributes()["fagan.namespace"] == "a b"


def test_namespace_cannot_be_influenced_by_request_data():
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    telemetry.record_lifecycle_event(
        {
            "type": "agent_dispatched",
            "plan": "evil-ns",
            "story_key": "OT-1",
            "payload": {"outcome": "merged"},
        }
    )

    assert exporter.get_finished_spans()
    assert telemetry._resource_attributes()["fagan.namespace"] == "default"


def test_configured_provider_exports_the_namespace_and_the_process_instance_id(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "fagan.namespace=acme")
    exporter = _memory_exporter()
    telemetry.configure(exporter=exporter)

    telemetry.record_lifecycle_event(
        {"type": "parked", "plan": "demo", "story_key": "OT-2", "payload": {}}
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    resource = spans[0].resource
    assert resource.attributes["fagan.namespace"] == "acme"
    assert resource.attributes["service.name"] == "fagan"
    assert resource.attributes["service.instance.id"] == (
        telemetry._resource_attributes()["service.instance.id"]
    )
