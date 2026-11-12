"""TDD contract for the test-scope dispatch counter and time-to-done histogram.

``pipeline.telemetry_test_scope`` adds two instruments that carry only the
bounded ``fagan.test_scope`` (``full`` / ``scoped``) and ``fagan.rework``
(``none`` / ``ci`` / ``review``) labels:

* ``fagan.story.dispatches`` -- counter, one per ``agent_dispatched``;
* ``fagan.story.dispatch_to_done.duration`` -- histogram, first dispatch to
  ``story_done``, labelled with the scope in force at the first dispatch.

Plan and story key are deliberately NOT labels (cardinality), so no attribute
set may contain them. The catalogue rows in ``docs/TELEMETRY_CONTRACT.md`` are
pinned here too, so the documented label set cannot drift from the code.

The reader idiom is the one from ``tests/unit/test_telemetry_durations``: a real
``MeterProvider`` with an ``InMemoryMetricReader``; no OTLP endpoint is contacted.
"""

# ruff: noqa: PLR0402
# The alias form is intentional: each test imports the recorder inside its body so
# a missing seam fails that test at run time, not collection for the whole module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle - see module docstring)

pytest.importorskip("opentelemetry.sdk.metrics.export")

METRICS_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

DISPATCHES = "fagan.story.dispatches"
DISPATCH_TO_DONE = "fagan.story.dispatch_to_done.duration"

PLAN = "scope-demo"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
THIRTY_DAYS = 30 * 24 * 3600
FORBIDDEN_ATTRIBUTES = {"fagan.plan", "fagan.story_key", "correlation_id"}

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "TELEMETRY_CONTRACT.md"
DISPATCHES_ROW = (
    "| `fagan.story.dispatches` | `{dispatch}` | `fagan.test_scope` (`full`, `scoped`), "
    "`fagan.rework` (`none`, `ci`, `review`) | `customer` and `harness` |"
)
DISPATCH_TO_DONE_ROW = (
    "| `fagan.story.dispatch_to_done.duration` | `s` | `fagan.test_scope` (`full`, `scoped`) "
    "— scope at the story's first dispatch | `customer` and `harness` |"
)
CONTRACT_SENTENCE = (
    "The two test-scope instruments carry only the bounded `fagan.test_scope` and "
    "`fagan.rework` labels, so rework rate (`rework` != `none` over all dispatches) "
    "and time-to-done can be compared by scope."
)


class Harness(NamedTuple):
    """The reader a test collects from plus the provider it must shut down."""

    reader: object
    provider: object


def _at(seconds: float = 0) -> str:
    """An ISO-8601 UTC timestamp *seconds* after the fixture start ``T0``."""
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _event(
    event_type: str,
    ts: str,
    *,
    story_key: str = "S1",
    payload: dict | None = None,
) -> dict:
    """A lifecycle event shaped exactly like ``events.make_event`` output."""
    return {
        "type": event_type,
        "plan": PLAN,
        "story_key": story_key,
        "payload": {} if payload is None else payload,
        "ts": ts,
    }


def _dispatch(
    ts: str, *, scope: str = "full", rework: str = "none", story_key: str = "S1"
) -> dict:
    """An ``agent_dispatched`` event carrying the test-scope payload fields."""
    return _event(
        "agent_dispatched",
        ts,
        story_key=story_key,
        payload={"test_scope": scope, "rework": rework},
    )


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
        {
            "count": getattr(point, "count", None),
            "sum": getattr(point, "sum", None),
            "value": getattr(point, "value", None),
            "attributes": dict(point.attributes),
        }
        for point in _points(reader, name)
    ]


def _recorded(reader: object, name: str) -> tuple[int, float]:
    """Total sample count and sum recorded under histogram *name*."""
    samples = _samples(reader, name)
    return (
        sum(sample["count"] for sample in samples),
        sum(sample["sum"] for sample in samples),
    )


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Isolate the module-level instrument/start state between tests."""
    import pipeline.telemetry_test_scope as test_scope

    test_scope.reset()
    yield
    test_scope.reset()


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


@pytest.fixture
def scope_meter() -> Harness:
    """Register only the test-scope instruments on an in-memory meter."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import pipeline.telemetry_test_scope as test_scope

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        test_scope.register(provider.get_meter("test-scope-unit"))
        yield Harness(reader, provider)
    finally:
        provider.shutdown()


def _observe(*events: dict) -> None:
    """Feed *events* straight into the test-scope recorder."""
    import pipeline.telemetry_test_scope as test_scope

    for event in events:
        test_scope.observe(event)


def test_dispatch_then_done_records_scoped_duration(scope_meter: Harness) -> None:
    """A scoped dispatch at t0 and ``story_done`` at t0+90s records 90.0 under ``scoped``."""
    _observe(_dispatch(_at(0), scope="scoped"), _event("story_done", _at(90)))

    samples = _samples(scope_meter.reader, DISPATCH_TO_DONE)
    assert len(samples) == 1
    assert samples[0]["count"] == 1
    assert samples[0]["sum"] == pytest.approx(90.0)
    assert samples[0]["attributes"] == {"fagan.test_scope": "scoped"}


def test_full_scope_dispatch_labels_the_duration_full(scope_meter: Harness) -> None:
    """The duration label is the scope of the dispatch, not a default."""
    _observe(_dispatch(_at(0), scope="full"), _event("story_done", _at(60)))

    samples = _samples(scope_meter.reader, DISPATCH_TO_DONE)
    assert samples[0]["attributes"] == {"fagan.test_scope": "full"}


@pytest.mark.parametrize("rework", ["none", "ci", "review"])
def test_dispatch_counter_carries_rework_label(
    scope_meter: Harness, rework: str
) -> None:
    """Each dispatch adds one to the counter under its rework label and scope."""
    _observe(_dispatch(_at(0), scope="scoped", rework=rework))

    samples = _samples(scope_meter.reader, DISPATCHES)
    assert len(samples) == 1
    assert samples[0]["value"] == 1
    assert samples[0]["attributes"] == {
        "fagan.test_scope": "scoped",
        "fagan.rework": rework,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"test_scope": "bogus", "rework": "none"},
        {"test_scope": "FULL", "rework": "none"},
        {"test_scope": None, "rework": "none"},
        {"test_scope": "full", "rework": "merge"},
        {"test_scope": "full", "rework": 3},
    ],
)
def test_unknown_payload_values_fold_to_safe_labels(
    scope_meter: Harness, payload: dict
) -> None:
    """Values outside the bounded sets fold to ``full`` / ``none``, never a new series."""
    _observe(_event("agent_dispatched", _at(0), payload=payload))

    samples = _samples(scope_meter.reader, DISPATCHES)
    assert samples[0]["attributes"] == {
        "fagan.test_scope": "full",
        "fagan.rework": "none",
    }


def test_missing_test_scope_counts_as_full(scope_meter: Harness) -> None:
    """Dispatch events from before this plan carry no scope and count as ``full``."""
    _observe(_event("agent_dispatched", _at(0), payload={}))

    samples = _samples(scope_meter.reader, DISPATCHES)
    assert samples[0]["attributes"] == {
        "fagan.test_scope": "full",
        "fagan.rework": "none",
    }


def test_rework_dispatch_does_not_move_the_start(scope_meter: Harness) -> None:
    """A later rework dispatch keeps the first dispatch's timestamp and scope."""
    _observe(
        _dispatch(_at(0), scope="scoped"),
        _dispatch(_at(60), scope="full", rework="ci"),
        _event("story_done", _at(90)),
    )

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (1, pytest.approx(90.0))
    samples = _samples(scope_meter.reader, DISPATCH_TO_DONE)
    assert samples[0]["attributes"] == {"fagan.test_scope": "scoped"}


def test_rework_dispatch_still_counts_toward_dispatches(scope_meter: Harness) -> None:
    """The rework dispatch is a dispatch: it adds to the counter labelled ``ci``."""
    _observe(
        _dispatch(_at(0), scope="scoped"),
        _dispatch(_at(60), scope="scoped", rework="ci"),
    )

    attributes = [
        sample["attributes"] for sample in _samples(scope_meter.reader, DISPATCHES)
    ]
    assert {"fagan.test_scope": "scoped", "fagan.rework": "none"} in attributes
    assert {"fagan.test_scope": "scoped", "fagan.rework": "ci"} in attributes


def test_story_done_without_dispatch_records_nothing(scope_meter: Harness) -> None:
    """A process that missed the dispatch invents no time-to-done sample."""
    _observe(_event("story_done", _at(90)))

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (0, 0)


def test_story_done_forgets_the_start(scope_meter: Harness) -> None:
    """A second ``story_done`` for the same story records nothing more."""
    _observe(
        _dispatch(_at(0), scope="scoped"),
        _event("story_done", _at(90)),
        _event("story_done", _at(120)),
    )

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (1, pytest.approx(90.0))


def test_done_for_another_story_does_not_close_this_one(scope_meter: Harness) -> None:
    """Starts are keyed per story: another story's done leaves this start open."""
    _observe(
        _dispatch(_at(0), scope="scoped", story_key="S1"),
        _event("story_done", _at(90), story_key="S2"),
    )

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (0, 0)


@pytest.mark.parametrize("done_offset", [-1, THIRTY_DAYS + 1])
def test_out_of_range_durations_are_dropped(
    scope_meter: Harness, done_offset: float
) -> None:
    """A negative gap or one over 30 days is bad data and records nothing."""
    _observe(_dispatch(_at(0), scope="scoped"), _event("story_done", _at(done_offset)))

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (0, 0)


def test_duration_at_the_thirty_day_bound_is_kept(scope_meter: Harness) -> None:
    """Exactly 30 days is inside the ``[0, 30 days]`` window and is recorded."""
    _observe(_dispatch(_at(0), scope="scoped"), _event("story_done", _at(THIRTY_DAYS)))

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (
        1,
        pytest.approx(THIRTY_DAYS),
    )


def _dispatch_distinct_stories(count: int) -> None:
    """Dispatch *count* stories ``S0`` .. ``S{count - 1}`` at ``T0``."""
    _observe(
        *(
            _dispatch(_at(0), scope="scoped", story_key=f"S{index}")
            for index in range(count)
        )
    )


def test_start_map_is_bounded_to_512_stories(scope_meter: Harness) -> None:
    """The 513th distinct story evicts the oldest start, keeping the newest 512."""
    _dispatch_distinct_stories(513)

    # S0's start was evicted, so its done records nothing. S1 is still tracked.
    _observe(_event("story_done", _at(90), story_key="S0"))
    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (0, 0)

    _observe(_event("story_done", _at(90), story_key="S1"))
    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (1, pytest.approx(90.0))


def test_reset_forgets_pending_starts(scope_meter: Harness) -> None:
    """``reset`` drops every start, so a done after it records nothing."""
    import pipeline.telemetry_test_scope as test_scope

    _observe(_dispatch(_at(0), scope="scoped"))
    test_scope.reset()
    _observe(_event("story_done", _at(90)))

    assert _recorded(scope_meter.reader, DISPATCH_TO_DONE) == (0, 0)


def test_observe_without_register_records_nothing_and_never_raises() -> None:
    """With telemetry disabled (no instruments) the recorder is a silent no-op."""
    import pipeline.telemetry_test_scope as test_scope

    test_scope.reset()
    _observe(_dispatch(_at(0), scope="scoped"), _event("story_done", _at(90)))


@pytest.mark.parametrize(
    "garbage",
    [
        {"type": "agent_dispatched", "payload": None},
        {
            "type": "agent_dispatched",
            "plan": PLAN,
            "story_key": "S1",
            "payload": "x",
            "ts": None,
        },
        {"type": "story_done", "plan": PLAN, "story_key": "S1", "ts": 12345},
        {
            "type": "agent_dispatched",
            "plan": PLAN,
            "story_key": "S1",
            "payload": {},
            "ts": "x",
        },
        {},
    ],
)
def test_observe_never_raises_on_garbage(scope_meter: Harness, garbage: dict) -> None:
    """Malformed events are dropped; the caller is never handed an exception."""
    _observe(garbage)


@pytest.mark.parametrize("name", [DISPATCHES, DISPATCH_TO_DONE])
def test_no_series_carries_plan_story_or_correlation_labels(
    scope_meter: Harness, name: str
) -> None:
    """Plan, story key and correlation id never become attributes on either instrument."""
    _observe(
        _dispatch(_at(0), scope="scoped", rework="review"),
        _event("story_done", _at(90)),
    )

    attributes = [
        set(sample["attributes"]) for sample in _samples(scope_meter.reader, name)
    ]
    assert attributes
    assert not FORBIDDEN_ATTRIBUTES & set().union(*attributes)


def test_lifecycle_seam_feeds_the_scope_instruments(metrics: Harness) -> None:
    """The real ``record_lifecycle_event`` seam reaches both new instruments."""
    import pipeline.telemetry as telemetry

    telemetry.record_lifecycle_event(_dispatch(_at(0), scope="scoped", rework="ci"))
    telemetry.record_lifecycle_event(_event("story_done", _at(90)))

    assert _recorded(metrics.reader, DISPATCH_TO_DONE) == (1, pytest.approx(90.0))
    dispatch_samples = _samples(metrics.reader, DISPATCHES)
    assert dispatch_samples[0]["attributes"] == {
        "fagan.test_scope": "scoped",
        "fagan.rework": "ci",
    }


def test_contract_catalogue_pins_the_dispatch_rows() -> None:
    """The documented catalogue lists both instruments with their exact label sets."""
    text = CONTRACT.read_text(encoding="utf-8")

    assert DISPATCHES_ROW in text
    assert DISPATCH_TO_DONE_ROW in text


def test_contract_explains_the_scope_comparison() -> None:
    """The contract states that the two instruments compare rework and time-to-done by scope."""
    text = " ".join(CONTRACT.read_text(encoding="utf-8").split())

    assert CONTRACT_SENTENCE in text
