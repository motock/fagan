"""Every telemetry name this project exports lives under the ``fagan`` namespace.

Dashboards and alerts key on these strings, so they are a public contract: the
service name is exactly ``fagan``, every counter/gauge name and both span names
carry the ``fagan.`` prefix, and a lifecycle span's Fagan-owned attribute keys
do too.

The assertions are deliberately prefix-based: a later story may add a metric
constant or a span attribute without breaking this file, which only pins the
namespace invariant (never an exact key set or constant count).

``pipeline.server`` is imported before any other ``pipeline.*`` module: the
package has a pre-existing import cycle that this suite inherits (same idiom
as ``tests/unit/test_triage_cap_silent_skip.py``).
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
def _clean_telemetry(monkeypatch):
    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


def test_service_name_is_the_bare_fagan_namespace():
    assert telemetry.SERVICE_NAME == "fagan"


def test_every_metric_constant_is_fagan_prefixed():
    names = [n for n in dir(telemetry) if n.startswith("METRIC_")]

    for expected in (
        "METRIC_DISPATCH_OUTCOMES",
        "METRIC_AUTHZ_DECISIONS",
        "METRIC_MERGE_RESULTS",
        "METRIC_STORIES",
        "METRIC_CAPACITY_IN_USE",
    ):
        assert expected in names
    assert all(getattr(telemetry, name).startswith("fagan.") for name in names)


def test_span_names_are_fagan_prefixed():
    assert telemetry.REQUEST_SPAN_NAME.startswith("fagan.")
    assert telemetry.LIFECYCLE_SPAN_NAME.startswith("fagan.")


def test_lifecycle_span_attributes_are_fagan_prefixed():
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
                "provider": "anthropic",
            },
        }
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == telemetry.LIFECYCLE_SPAN_NAME]
    assert len(spans) == 1
    attributes = spans[0].attributes

    assert [k for k in attributes if k.startswith("pipeline.")] == []
    fagan_keys = [k for k in attributes if k.startswith("fagan.")]
    assert fagan_keys
    assert all(k.startswith("fagan.") for k in fagan_keys)
