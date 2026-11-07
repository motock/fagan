"""TDD contract for harness-profile usage metrics (``pipeline.telemetry_usage``).

``pipeline.telemetry_usage`` turns one usage record into three OpenTelemetry
instruments, and only under the ``harness`` deployment profile:

* ``fagan.usage.tokens`` (counter, unit ``{token}``) with ``kind`` in
  {input, output, cache_creation, cache_read}, plus role, backend and model;
* ``fagan.usage.cost`` (counter, unit ``USD``) with role, backend and model;
* ``fagan.usage.call.duration`` (histogram, unit ``ms``) with role and backend.

The invariants pinned here: zero is a real measurement (a full cache hit, a
free call) and must not be dropped by a truthiness guard; a record whose role
or backend is not a short ``[A-Za-z0-9._:-]+`` string is skipped *entirely*
before anything is emitted, so no malformed value can mint a series; the
``customer`` profile registers nothing and exports nothing; and the attribution
ids of the record (plan, story key, correlation id, attempt) are never
attributes. The usage JSONL remains the billing-grade source.

The reader idiom is the one from ``tests/unit/test_telemetry_durations``: a real
``MeterProvider`` with an ``InMemoryMetricReader`` -- no OTLP endpoint is ever
contacted. ``import pipeline.server`` comes first for the pre-existing import
cycle (same idiom as ``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

import pipeline.persistence
import pipeline.server  # pre-existing import cycle - see module docstring

pytest.importorskip("opentelemetry.sdk.metrics.export")

#: Deployment profile variable: ``harness`` is the only value that enables these.
PROFILE_ENV = "PIPELINE_TELEMETRY_PROFILE"
#: Standard OTLP metrics endpoint variable: set means "metrics are configured".
METRICS_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

TOKENS = "fagan.usage.tokens"
COST = "fagan.usage.cost"
DURATION = "fagan.usage.call.duration"
ALL_METRICS = (TOKENS, COST, DURATION)


class Harness(NamedTuple):
    """A meter provider plus the in-memory reader its metrics land in."""

    reader: object
    provider: object


def _configure(monkeypatch: pytest.MonkeyPatch, profile: str) -> Harness:
    """Configure metrics under *profile* against a fresh in-memory reader."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from pipeline import telemetry

    monkeypatch.setenv(PROFILE_ENV, profile)
    monkeypatch.setenv(METRICS_ENDPOINT_ENV, "http://localhost:4318/v1/metrics")
    telemetry.reset_metrics()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    assert telemetry.configure_metrics(provider) is True
    return Harness(reader, provider)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Isolate the module-level instruments between tests."""
    from pipeline import telemetry

    telemetry.reset_metrics()
    yield
    telemetry.reset_metrics()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """A ``harness``-profile provider exporting into an in-memory reader."""
    configured = _configure(monkeypatch, "harness")
    try:
        yield configured
    finally:
        configured.provider.shutdown()


@pytest.fixture
def customer(monkeypatch: pytest.MonkeyPatch):
    """A ``customer``-profile provider exporting into an in-memory reader."""
    configured = _configure(monkeypatch, "customer")
    try:
        yield configured
    finally:
        configured.provider.shutdown()


def _record(**overrides: object) -> dict:
    """A usage record shaped exactly like ``usage_record.record_usage``'s."""
    record: dict = {
        "ts": "2026-11-05T10:12:33.412Z",
        "plan": "plan-usage",
        "story_key": "USAGE-1",
        "attempt": 2,
        "correlation_id": "0f9c4b7e",
        "role": "usage-main",
        "backend": "claude",
        "model": "m-requested",
        "served_model": "m-served",
        "input_tokens": 1200,
        "output_tokens": 350,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 800,
        "cost_usd": 0.0042,
        "duration_ms": 12.5,
    }
    record.update(overrides)
    return record


def _metric(reader: object, name: str) -> object | None:
    """The collected metric named *name*, or ``None`` when nothing was recorded."""
    data = reader.get_metrics_data()
    if data is None:
        return None
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return metric
    return None


def _points(reader: object, name: str) -> list:
    """Every data point currently collected for metric *name*."""
    metric = _metric(reader, name)
    return [] if metric is None else list(metric.data.data_points)


def _samples(reader: object, name: str) -> list[dict]:
    """Value/sum plus attributes of every *name* point."""
    samples: list[dict] = []
    for point in _points(reader, name):
        attributes = dict(point.attributes)
        samples.append(
            {
                "value": getattr(point, "value", None),
                "count": getattr(point, "count", None),
                "sum": getattr(point, "sum", None),
                "attributes": attributes,
            }
        )
    return samples


def _for_role(reader: object, name: str, role: str) -> list[dict]:
    """The *name* samples whose ``role`` attribute is *role*."""
    return [s for s in _samples(reader, name) if s["attributes"].get("role") == role]


def _tokens_by_kind(reader: object, role: str) -> dict:
    """``{kind: value}`` for the token points of *role*."""
    return {s["attributes"]["kind"]: s["value"] for s in _for_role(reader, TOKENS, role)}


def test_harness_record_exports_tokens_cost_and_duration(harness: Harness) -> None:
    """One record yields four token kinds, one cost sample and one duration."""
    from pipeline import telemetry_usage

    role = "usage-main-export"
    telemetry_usage.record(_record(role=role, served_model="m-served"))

    assert _tokens_by_kind(harness.reader, role) == {
        "input": 1200,
        "output": 350,
        "cache_read": 800,
        "cache_creation": 0,
    }
    for sample in _for_role(harness.reader, TOKENS, role):
        assert sample["attributes"]["backend"] == "claude"
        assert sample["attributes"]["model"] == "m-served"

    costs = _for_role(harness.reader, COST, role)
    assert len(costs) == 1
    assert costs[0]["value"] == pytest.approx(0.0042)
    assert costs[0]["attributes"]["backend"] == "claude"
    assert costs[0]["attributes"]["model"] == "m-served"

    durations = _for_role(harness.reader, DURATION, role)
    assert len(durations) == 1
    assert durations[0]["count"] == 1
    assert durations[0]["sum"] == pytest.approx(12.5)
    assert durations[0]["attributes"]["backend"] == "claude"

    assert _metric(harness.reader, TOKENS).unit == "{token}"
    assert _metric(harness.reader, COST).unit == "USD"
    assert _metric(harness.reader, DURATION).unit == "ms"


def test_missing_served_model_falls_back_to_the_requested_model(harness: Harness) -> None:
    """``served_model=None`` reports the requested ``model``, not ``None``."""
    from pipeline import telemetry_usage

    role = "usage-served-model-fallback"
    telemetry_usage.record(
        _record(role=role, model="m-requested", served_model=None)
    )

    token_samples = _for_role(harness.reader, TOKENS, role)
    cost_samples = _for_role(harness.reader, COST, role)
    assert token_samples and cost_samples
    for sample in token_samples + cost_samples:
        assert sample["attributes"]["model"] == "m-requested"


def test_customer_profile_registers_and_exports_no_usage_metrics(
    customer: Harness,
) -> None:
    """The default profile creates none of the three instruments."""
    from pipeline import telemetry_usage

    for name in ALL_METRICS:
        assert _metric(customer.reader, name) is None

    role = "usage-customer-negative"
    assert telemetry_usage.record(_record(role=role)) is None

    for name in ALL_METRICS:
        assert _for_role(customer.reader, name, role) == []


def test_missing_cost_and_duration_skip_only_those_samples(harness: Harness) -> None:
    """A ``None`` cost or duration omits that sample and keeps the token points."""
    from pipeline import telemetry_usage

    role = "usage-partial-nulls"
    telemetry_usage.record(_record(role=role, cost_usd=None, duration_ms=None))

    assert _tokens_by_kind(harness.reader, role) == {
        "input": 1200,
        "output": 350,
        "cache_read": 800,
        "cache_creation": 0,
    }
    assert _for_role(harness.reader, COST, role) == []
    assert _for_role(harness.reader, DURATION, role) == []


def test_malformed_role_or_backend_skips_the_whole_record(harness: Harness) -> None:
    """A bad role/backend leaves no sample of any kind, and no residue."""
    from pipeline import telemetry_usage

    telemetry_usage.record(_record(role="backend dev"))
    telemetry_usage.record(_record(role="a" * 65))
    telemetry_usage.record(_record(backend="backend dev"))
    telemetry_usage.record(_record(backend="b" * 65))

    for name in ALL_METRICS:
        assert _samples(harness.reader, name) == []

    # No half-emitted residue: the next valid record sums exactly its own value.
    role = "backend-dev"
    telemetry_usage.record(_record(role=role))

    assert _tokens_by_kind(harness.reader, role)["input"] == 1200
    assert [s["value"] for s in _for_role(harness.reader, COST, role)] == [
        pytest.approx(0.0042)
    ]
    durations = _for_role(harness.reader, DURATION, role)
    assert len(durations) == 1
    assert durations[0]["count"] == 1
    assert durations[0]["sum"] == pytest.approx(12.5)


def test_role_of_exactly_64_characters_is_accepted(harness: Harness) -> None:
    """The length bound is inclusive: 64 valid characters still export."""
    from pipeline import telemetry_usage

    role = "c" * 64
    telemetry_usage.record(_record(role=role))

    assert _tokens_by_kind(harness.reader, role)["input"] == 1200


def test_negative_or_non_numeric_token_counts_never_raise(harness: Harness) -> None:
    """A bad count is dropped or sanitized; nothing escapes ``record``."""
    from pipeline import telemetry_usage

    for index, bad in enumerate((-5, "abc", None)):
        role = f"usage-bad-count-{index}"
        assert telemetry_usage.record(_record(role=role, input_tokens=bad)) is None
        assert all(sample["value"] >= 0 for sample in _for_role(harness.reader, TOKENS, role))


def test_exported_attributes_carry_no_attribution_ids(harness: Harness) -> None:
    """Plan, story key, correlation id and attempt stay on the record."""
    from pipeline import telemetry_usage

    telemetry_usage.record(_record(role="usage-attributes"))

    forbidden = {"plan", "story_key", "correlation_id", "attempt"}
    seen = 0
    for name in ALL_METRICS:
        for sample in _samples(harness.reader, name):
            assert set(sample["attributes"]).isdisjoint(forbidden)
            seen += 1
    assert seen > 0


def test_record_usage_survives_a_failing_metrics_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSONL line is still written when the metric export raises."""
    from pipeline import telemetry_usage
    from pipeline.usage_record import record_usage, usage_scope

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)

    def _explode(_usage_record: dict) -> None:
        raise RuntimeError("metrics recording exploded")

    monkeypatch.setattr(telemetry_usage, "record", _explode)

    sidecar = tmp_path / "plan-usage-jsonl.usage.jsonl"

    with usage_scope("plan-usage-jsonl", "USAGE-1", attempt=1):
        assert (
            record_usage(role="dispatch", backend="claude", model="m", input_tokens=1200)
            is None
        )
    assert len(_jsonl(sidecar)) == 1

    with usage_scope("plan-usage-jsonl", "USAGE-1", attempt=2):
        assert (
            record_usage(role="dispatch", backend="claude", model="m", input_tokens=1200)
            is None
        )
    assert len(_jsonl(sidecar)) == 2


def _jsonl(path: Path) -> list[dict]:
    """Parse the usage sidecar at *path* (empty list when it is absent)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
