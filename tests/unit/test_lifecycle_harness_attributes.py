"""Harness-only attributes on the lifecycle span.

The deployment profile decides how much metadata a signal carries
(docs/TELEMETRY_CONTRACT.md, ``## Profiles`` and ``## Data classification``).
Under ``harness`` the ``agent_dispatched`` lifecycle span additionally carries
the dispatch role, the model and the attempt number; under ``customer`` the
attribute set is exactly what it is without this feature — no model, no token
counts, no cost.

The profile is a deployment setting read from the environment, never request
data: an event payload that claims ``profile: "harness"`` cannot select the
profile, so the assertions below are membership/absence only.

Everything runs against a real ``TracerProvider`` with an in-memory span
exporter -- no OTLP endpoint is contacted. The import of ``pipeline.server``
comes first: a pipeline module imported cold without it hits the pre-existing
import cycle (same idiom as ``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from pipeline import telemetry, telemetry_profile

PLAN = "harness-attributes"
STORY = "harn-1"
CID = "corr-harness-attributes"
MODEL = "claude-sonnet-4-5"

# The four attributes this feature can add; every negative test asserts their
# absence one by one rather than asserting on the whole attribute dictionary.
HARNESS_KEYS = ("fagan.role", "fagan.model", "fagan.attempt")

ENDPOINT_ENVS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)


@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(telemetry_profile.PROFILE_ENV, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


def _use_profile(monkeypatch, name):
    """Pin the deployment profile and prove the switch really took effect.

    Without this guard a test that silently stayed on ``customer`` would pass
    its negative assertions for the wrong reason.
    """
    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, name)
    if name == telemetry_profile.HARNESS:
        assert telemetry_profile.is_harness() is True
    else:
        assert telemetry_profile.is_harness() is False


@pytest.fixture
def exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    memory = InMemorySpanExporter()
    telemetry.configure(memory)
    return memory


def _record(event_type="agent_dispatched", payload=None):
    telemetry.record_lifecycle_event(
        {
            "type": event_type,
            "plan": PLAN,
            "story_key": STORY,
            "correlation_id": CID,
            "payload": payload if payload is not None else {},
        }
    )


def _only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def test_harness_dispatch_span_carries_role_model_and_attempt(exporter, monkeypatch):
    _use_profile(monkeypatch, telemetry_profile.HARNESS)
    _record(
        payload={"role": "dispatch", "model": MODEL, "attempt": 2},
    )

    attributes = _only_span(exporter).attributes
    assert attributes["fagan.role"] == "dispatch"
    assert attributes["fagan.model"] == MODEL
    assert attributes["fagan.attempt"] == 2


def test_customer_dispatch_span_has_no_harness_attributes(exporter, monkeypatch):
    _use_profile(monkeypatch, telemetry_profile.CUSTOMER)
    _record(
        payload={"role": "dispatch", "model": MODEL, "attempt": 2},
    )

    attributes = _only_span(exporter).attributes
    for key in HARNESS_KEYS:
        assert key not in attributes


def test_harness_non_dispatch_event_has_no_harness_attributes(exporter, monkeypatch):
    _use_profile(monkeypatch, telemetry_profile.HARNESS)
    _record(
        event_type="agent_done",
        payload={"role": "dispatch", "model": MODEL, "attempt": 2},
    )

    attributes = _only_span(exporter).attributes
    for key in HARNESS_KEYS:
        assert key not in attributes


@pytest.mark.parametrize("attempt", ["2", -1, None, 1.5, True])
def test_harness_dispatch_omits_unusable_attempt_but_keeps_role_and_model(
    exporter, monkeypatch, attempt
):
    _use_profile(monkeypatch, telemetry_profile.HARNESS)
    _record(
        payload={"role": "dispatch", "model": MODEL, "attempt": attempt},
    )

    attributes = _only_span(exporter).attributes
    assert attributes["fagan.role"] == "dispatch"
    assert attributes["fagan.model"] == MODEL
    assert "fagan.attempt" not in attributes


def test_harness_dispatch_keeps_attempt_zero(exporter, monkeypatch):
    _use_profile(monkeypatch, telemetry_profile.HARNESS)
    _record(
        payload={"role": "dispatch", "model": MODEL, "attempt": 0},
    )

    attributes = _only_span(exporter).attributes
    assert attributes["fagan.attempt"] == 0


def test_payload_cannot_choose_the_profile(exporter, monkeypatch):
    _use_profile(monkeypatch, telemetry_profile.CUSTOMER)
    _record(
        payload={
            "profile": "harness",
            "role": "dispatch",
            "model": MODEL,
            "attempt": 2,
        },
    )

    attributes = _only_span(exporter).attributes
    for key in HARNESS_KEYS:
        assert key not in attributes
