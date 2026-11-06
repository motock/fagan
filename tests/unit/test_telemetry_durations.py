"""TDD contract for the story lifecycle duration histograms.

``pipeline.telemetry_durations`` turns the lifecycle events already published on
the bus into three label-free histograms measured in seconds:

* ``fagan.story.dispatch_to_pr.duration`` -- ``agent_dispatched`` -> ``pr_open``;
* ``fagan.story.pr_to_ci.duration`` -- ``pr_open`` -> ``ci_complete``;
* ``fagan.story.pr_to_done.duration`` -- ``pr_open`` -> ``story_done``.

Telemetry is lossy by contract: a process that never saw the start event
records nothing for that story, and no bad timestamp may ever raise out of the
recording seam. Plan and story key are deliberately NOT labels (cardinality),
so every data point carries an empty attribute dict and only the resource
attributes identify the deployment.

The reader idiom is the one from ``tests/unit/test_pipeline_telemetry_metrics``:
a real ``MeterProvider`` with an ``InMemoryMetricReader`` -- no OTLP endpoint is
ever contacted. ``import pipeline.server`` comes first for the pre-existing
import cycle (same idiom as ``tests/unit/test_triage_cap_silent_skip.py``).
"""

# ruff: noqa: PLR0402
# The alias form is intentional: ``import pipeline.telemetry as telemetry`` is
# repeated inside each test body so a recording seam the implementation has not
# grown yet fails that test at run time, not collection for the whole module.

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle - see module docstring)

pytest.importorskip("opentelemetry.sdk.metrics.export")

#: Standard OTLP metrics endpoint variable: set means "metrics are configured".
METRICS_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"
LEGACY_ENDPOINT_ENV = "PIPELINE_OTEL_ENDPOINT"

DISPATCH_TO_PR = "fagan.story.dispatch_to_pr.duration"
PR_TO_CI = "fagan.story.pr_to_ci.duration"
PR_TO_DONE = "fagan.story.pr_to_done.duration"
ALL_METRICS = (DISPATCH_TO_PR, PR_TO_CI, PR_TO_DONE)

PLAN = "duration-demo"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: Subprocess script: import and use the recorder with no endpoint configured,
#: then report every ``opentelemetry`` module the import pulled in.
_ISOLATION_SCRIPT = (
    "import sys\n"
    "import pipeline.telemetry_durations as durations\n"
    "start = {'type': 'agent_dispatched', 'plan': 'p', 'story_key': 'S1',"
    " 'payload': {}, 'ts': '2026-01-01T00:00:00+00:00'}\n"
    "end = {'type': 'pr_open', 'plan': 'p', 'story_key': 'S1',"
    " 'payload': {}, 'ts': '2026-01-01T00:01:30+00:00'}\n"
    "durations.observe(start)\n"
    "durations.observe(end)\n"
    "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')))\n"
)


class Harness(NamedTuple):
    """The reader a test collects from plus the provider it must shut down."""

    reader: object
    provider: object


def _at(seconds: float = 0) -> str:
    """An ISO-8601 UTC timestamp *seconds* after the fixture start ``T0``."""
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _event(event_type: str, ts: str, *, story_key: str = "S1") -> dict:
    """A lifecycle event shaped exactly like ``events.make_event`` output."""
    return {
        "type": event_type,
        "plan": PLAN,
        "story_key": story_key,
        "payload": {},
        "ts": ts,
    }


def _points(reader: object, name: str) -> list:
    """Every data point currently collected for metric *name*."""
    data = reader.get_metrics_data()
    points: list = []
    if data is None:
        return points
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def _samples(reader: object, name: str) -> list[dict]:
    """The ``count``, ``sum`` and data-point attributes of every *name* point."""
    return [
        {"count": point.count, "sum": point.sum, "attributes": dict(point.attributes)}
        for point in _points(reader, name)
    ]


def _recorded(reader: object, name: str) -> tuple[int, float]:
    """Total sample count and sum recorded under *name* across all points."""
    return (
        sum(sample["count"] for sample in _samples(reader, name)),
        sum(sample["sum"] for sample in _samples(reader, name)),
    )


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Isolate the module-level instrument/start state between tests."""
    import pipeline.telemetry as telemetry

    # ``getattr`` so a missing reset surfaces inside each test body (as the
    # missing recorder it stands for) rather than as a fixture setup error.
    reset = getattr(telemetry, "reset_metrics", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


@pytest.fixture
def metrics(monkeypatch) -> Harness:
    """Configure the metrics against an in-memory reader with the switch on."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import pipeline.telemetry as telemetry

    monkeypatch.setenv(METRICS_ENDPOINT_ENV, "http://localhost:4318/v1/metrics")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    assert telemetry.configure_metrics(provider) is True
    try:
        yield Harness(reader, provider)
    finally:
        provider.shutdown()


def _record(*events: dict) -> None:
    """Publish *events* through the real lifecycle recording seam."""
    import pipeline.telemetry as telemetry

    for event in events:
        telemetry.record_lifecycle_event(event)


def test_dispatch_to_pr_duration_is_recorded(metrics: Harness) -> None:
    """``agent_dispatched`` at t0 and ``pr_open`` at t0+90s record one 90.0 sample."""
    _record(_event("agent_dispatched", _at(0)), _event("pr_open", _at(90)))

    samples = _samples(metrics.reader, DISPATCH_TO_PR)
    assert len(samples) == 1
    assert samples[0]["count"] == 1
    assert samples[0]["sum"] == pytest.approx(90.0)


def test_pr_to_ci_duration_is_recorded(metrics: Harness) -> None:
    """``pr_open`` then ``ci_complete`` 30s later records 30.0."""
    _record(_event("pr_open", _at(0)), _event("ci_complete", _at(30)))

    samples = _samples(metrics.reader, PR_TO_CI)
    assert len(samples) == 1
    assert samples[0]["count"] == 1
    assert samples[0]["sum"] == pytest.approx(30.0)
    # Without a dispatch there is no dispatch-to-PR sample to invent.
    assert _recorded(metrics.reader, DISPATCH_TO_PR) == (0, 0)


def test_pr_to_done_duration_is_recorded(metrics: Harness) -> None:
    """``pr_open`` then ``story_done`` 600s later records 600.0."""
    _record(_event("pr_open", _at(0)), _event("story_done", _at(600)))

    samples = _samples(metrics.reader, PR_TO_DONE)
    assert len(samples) == 1
    assert samples[0]["count"] == 1
    assert samples[0]["sum"] == pytest.approx(600.0)


def test_pr_open_without_dispatch_records_nothing(metrics: Harness) -> None:
    """A process that missed ``agent_dispatched`` invents no duration."""
    _record(_event("pr_open", _at(0)))

    assert _recorded(metrics.reader, DISPATCH_TO_PR) == (0, 0)


def test_pr_open_starts_both_later_pairs(metrics: Harness) -> None:
    """One ``pr_open`` feeds the CI and the done pair, label-free each time."""
    _record(
        _event("pr_open", _at(0)),
        _event("ci_complete", _at(30)),
        _event("story_done", _at(600)),
    )

    assert _recorded(metrics.reader, PR_TO_CI) == (1, pytest.approx(30.0))
    assert _recorded(metrics.reader, PR_TO_DONE) == (1, pytest.approx(600.0))


@pytest.mark.parametrize(
    "start_ts, end_ts",
    [
        (_at(0), _at(-1)),  # end before start -> negative duration
        (_at(0), _at(31 * 24 * 3600)),  # 31 days -> over the 30-day bound
        (_at(0), "not-a-timestamp"),  # malformed end timestamp
        ("", _at(90)),  # malformed start timestamp
    ],
)
def test_invalid_durations_are_dropped_silently(
    metrics: Harness, start_ts: str, end_ts: str
) -> None:
    """Out-of-range and unparseable pairs record nothing and never raise."""
    _record(_event("agent_dispatched", start_ts)) if start_ts else None
    _record(_event("pr_open", end_ts))

    assert _recorded(metrics.reader, DISPATCH_TO_PR) == (0, 0)


def test_histograms_carry_no_labels(metrics: Harness) -> None:
    """Plan, story key and correlation id never become metric labels."""
    events = [
        _event("agent_dispatched", _at(0)),
        _event("pr_open", _at(90)),
        _event("ci_complete", _at(120)),
        _event("story_done", _at(690)),
    ]
    for event in events:
        event["correlation_id"] = "corr-1"
    _record(*events)

    for name in ALL_METRICS:
        samples = _samples(metrics.reader, name)
        assert samples, f"{name} recorded nothing"
        for sample in samples:
            assert sample["attributes"] == {}
            assert "fagan.plan" not in sample["attributes"]
            assert "fagan.story_key" not in sample["attributes"]
            assert "correlation_id" not in sample["attributes"]


def test_start_map_is_bounded_to_512_stories(metrics: Harness) -> None:
    """The 513th distinct story evicts the oldest start, keeping the newest 512."""
    for index in range(513):
        _record(_event("agent_dispatched", _at(0), story_key=f"S{index}"))

    # S0's start was evicted, so its PR records nothing. S1 is still tracked.
    _record(_event("pr_open", _at(90), story_key="S0"))
    assert _recorded(metrics.reader, DISPATCH_TO_PR) == (0, 0)

    _record(_event("pr_open", _at(90), story_key="S1"))
    count, total = _recorded(metrics.reader, DISPATCH_TO_PR)
    assert count == 1
    assert total == pytest.approx(90.0)


def test_reset_metrics_forgets_pending_starts(metrics: Harness) -> None:
    """``reset_metrics`` drops the start map; a fresh dispatch records normally."""
    import pipeline.telemetry as telemetry

    _record(_event("agent_dispatched", _at(0)))
    telemetry.reset_metrics()
    _record(_event("pr_open", _at(90)))

    assert _recorded(metrics.reader, DISPATCH_TO_PR) == (0, 0)


def test_disabled_telemetry_records_nothing(monkeypatch) -> None:
    """With no endpoint configured ``observe`` is a silent no-op."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import pipeline.telemetry as telemetry
    import pipeline.telemetry_durations as durations

    for name in (METRICS_ENDPOINT_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT", LEGACY_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    telemetry.reset_metrics()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        assert telemetry.configure_metrics(provider) is False
        _record(_event("agent_dispatched", _at(0)), _event("pr_open", _at(90)))
        durations.observe(_event("pr_open", _at(180)))

        data = reader.get_metrics_data()
        names: set[str] = set()
        if data is not None:
            for resource_metrics in data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    names.update(metric.name for metric in scope_metrics.metrics)
        assert not [name for name in names if name.startswith("fagan.")]
    finally:
        provider.shutdown()


def test_disabled_module_imports_no_opentelemetry() -> None:
    """Importing and using the recorder alone never imports ``opentelemetry``."""
    root = Path(__file__).resolve().parents[2]
    script = _ISOLATION_SCRIPT
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OTEL_", "PIPELINE_OTEL_"))
    }
    env["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
