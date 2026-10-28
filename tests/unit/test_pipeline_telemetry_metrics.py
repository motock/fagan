"""TDD contract for the opt-in OpenTelemetry metrics in ``pipeline.telemetry``.

The metrics are the counters and gauges an operator alerts on:

* ``pipeline.dispatch.outcomes{outcome}`` -- one record per dispatch attempt;
* ``pipeline.authz.decisions{action,outcome}`` -- one record per authorization
  decision, carrying NO principal/subject label (unbounded cardinality and
  PII, so the identity never leaves the process);
* ``pipeline.merge.results{result}`` -- one record per adjudicated story;
* ``pipeline.stories{status}`` and ``pipeline.capacity.in_use`` -- observable
  gauges fed by a snapshot the scheduler tick publishes once per scan.

Everything here runs against a real ``MeterProvider`` with an
``InMemoryMetricReader`` -- no OTLP endpoint is ever contacted -- and the whole
module skips when the SDK is not installed, because the disabled path is a
no-op that must never import ``opentelemetry``.

The import of ``pipeline.server`` comes first: a pipeline module imported cold
without it hits the pre-existing import cycle (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

# ruff: noqa: I001, PLR0402
# Import sorting is disabled because ``import pipeline.telemetry as telemetry``
# is repeated inside each test body on purpose: a recorder the implementation
# has not grown yet must fail that test at run time, not break collection for
# the whole module.

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle - see module docstring)

pytest.importorskip("opentelemetry.sdk.metrics.export")

ENDPOINT_ENV = "PIPELINE_OTEL_ENDPOINT"
PLAN = "metrics-demo"

#: Every label key the authz series may carry. Anything identity-shaped would
#: break the "no principal label" contract this module pins.
FORBIDDEN_LABEL_FRAGMENTS = ("principal", "subject", "actor", "user")


class Harness(NamedTuple):
    """The reader a test collects from plus the provider it must shut down."""

    reader: object
    provider: object


class _Bus:
    """Minimal EventBus stand-in for ``scan_all_plans``."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


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


def _value(reader: object, name: str, **attributes: str) -> int | float | None:
    """The collected value of the series identified by *attributes*, if any."""
    for point in _points(reader, name):
        if dict(point.attributes) == attributes:
            return point.value
    return None


def _metric_names(reader: object) -> set[str]:
    """Every metric name the reader currently reports (empty when it has none)."""
    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is None:
        return names
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            names.update(metric.name for metric in scope_metrics.metrics)
    return names


def _series(reader: object, name: str) -> dict[tuple[tuple[str, str], ...], object]:
    """All series of metric *name* keyed by their sorted attribute pairs."""
    return {
        tuple(sorted(dict(point.attributes).items())): point.value
        for point in _points(reader, name)
    }


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Isolate the module-level instrument/snapshot state between tests.

    Without this the counters accumulate across tests: test A recording one
    dispatch would make test B's "one event -> 1" assertion observe 2.
    """
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

    monkeypatch.setenv(ENDPOINT_ENV, "http://localhost:4318/v1/metrics")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    # ``getattr``/no assert so a not-yet-implemented recorder fails inside the
    # test body that names it, never as a fixture setup error.
    configure = getattr(telemetry, "configure_metrics", None)
    if configure is not None:
        configure(provider)
    try:
        yield Harness(reader, provider)
    finally:
        provider.shutdown()


def _write_manifest(plan_name: str, stories: dict[str, dict]) -> None:
    """Write *plan_name*'s manifest with the real manifest writer."""
    from pipeline.store import FileStore

    FileStore().save_manifest(plan_name, {"stories": stories})


def _tick(monkeypatch, plan_dir) -> list[dict]:
    """Run the real scan tick against the scratch manifests in *plan_dir*."""
    from pipeline import paths, watchers

    monkeypatch.setattr(paths, "PLAN_DIR", plan_dir)
    bus = _Bus()
    watchers.scan_all_plans(bus)
    return bus.events


def test_unset_endpoint_registers_nothing(monkeypatch) -> None:
    """No endpoint -> nothing registered: no ``pipeline.*`` series at all."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import pipeline.telemetry as telemetry

    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    telemetry.reset_metrics()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        assert telemetry.configure_metrics(provider) is False

        # Every recording seam still fires; all of them must stay silent.
        telemetry.record_dispatch_outcome("dispatched")
        telemetry.record_authz_decision("plan:dispatch", "allowed")
        telemetry.record_merge_result("merged")
        telemetry.update_manifest_snapshot({"in_progress": 1}, 1)

        names = _metric_names(reader)
        assert not [name for name in names if name.startswith("pipeline.")]
    finally:
        provider.shutdown()


def test_configure_is_idempotent(metrics: Harness) -> None:
    """A second configure on the same provider must not duplicate instruments."""
    import pipeline.telemetry as telemetry

    # Give every counter one series, then configure again: a duplicate
    # registration would show up as the same metric name appearing twice.
    telemetry.record_dispatch_outcome("dispatched")
    telemetry.record_authz_decision("plan:dispatch", "allowed")
    telemetry.record_merge_result("merged")
    telemetry.update_manifest_snapshot({}, 0)
    assert telemetry.configure_metrics(metrics.provider) is True

    counted: dict[str, int] = {}
    data = metrics.reader.get_metrics_data()
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                counted[metric.name] = counted.get(metric.name, 0) + 1

    expected = {
        telemetry.METRIC_DISPATCH_OUTCOMES,
        telemetry.METRIC_AUTHZ_DECISIONS,
        telemetry.METRIC_MERGE_RESULTS,
        telemetry.METRIC_STORIES,
        telemetry.METRIC_CAPACITY_IN_USE,
    }
    assert set(counted) == expected
    for name in expected:
        assert counted[name] == 1, f"{name} registered {counted[name]} times"


def test_dispatch_outcome_counter_increments_per_event(metrics: Harness) -> None:
    """One dispatch event -> one increment, on that outcome's series only."""
    import pipeline.telemetry as telemetry

    assert telemetry.DISPATCH_OUTCOMES == frozenset(
        {"dispatched", "skipped", "pending", "failed"}
    )

    telemetry.record_dispatch_outcome("dispatched")
    assert (
        _value(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES, outcome="dispatched")
        == 1
    )

    telemetry.record_dispatch_outcome("dispatched")
    assert (
        _value(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES, outcome="dispatched")
        == 2
    )

    # A different outcome is its own series; nothing else was invented.
    telemetry.record_dispatch_outcome("failed")
    assert (
        _value(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES, outcome="failed") == 1
    )
    assert set(_series(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES)) == {
        (("outcome", "dispatched"),),
        (("outcome", "failed"),),
    }


def test_authz_counter_has_no_principal_label(plan_dir, metrics: Harness, monkeypatch) -> None:
    """One allowed and one denied decision, labelled only by action + outcome."""
    from pipeline.actor import ANONYMOUS, bind_principal
    from pipeline.authorization import ENFORCE_ENV, authorize

    import pipeline.telemetry as telemetry

    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    # A non-operator principal is refused.
    with bind_principal(ANONYMOUS):
        assert authorize("plan:dispatch", plan_name=PLAN) is not None
    # The operator identity the scheduler runs as is allowed.
    with bind_principal(None):
        assert authorize("plan:dispatch", plan_name=PLAN) is None

    name = telemetry.METRIC_AUTHZ_DECISIONS
    assert _value(metrics.reader, name, action="plan:dispatch", outcome="denied") == 1
    assert _value(metrics.reader, name, action="plan:dispatch", outcome="allowed") == 1

    for point in _points(metrics.reader, name):
        attributes = dict(point.attributes)
        assert set(attributes) == {"action", "outcome"}
        blob = json.dumps(attributes).lower()
        for fragment in FORBIDDEN_LABEL_FRAGMENTS:
            assert fragment not in blob, f"{fragment!r} leaked into the authz labels"
        assert ANONYMOUS.subject.lower() not in blob


def test_authz_counter_accepts_every_action_enum_value(plan_dir, metrics: Harness, monkeypatch) -> None:
    """Every ``Action`` member is a valid label; no call site is out of enum."""
    from app.policy import Action

    import pipeline.telemetry as telemetry

    assert telemetry.AUTHZ_ACTIONS == frozenset(action.value for action in Action)

    telemetry.record_authz_decision(Action.STORY_MERGE, "allowed")
    assert (
        _value(
            metrics.reader,
            telemetry.METRIC_AUTHZ_DECISIONS,
            action="story:merge",
            outcome="allowed",
        )
        == 1
    )


def test_merge_result_counter_increments(metrics: Harness) -> None:
    """One adjudicated story -> one increment on its result's series."""
    import pipeline.telemetry as telemetry

    assert telemetry.MERGE_RESULTS == frozenset({"merged", "parked", "failed"})

    telemetry.record_merge_result("parked")
    telemetry.record_merge_result("parked")
    assert _value(metrics.reader, telemetry.METRIC_MERGE_RESULTS, result="parked") == 2

    telemetry.record_merge_result("merged")
    assert _value(metrics.reader, telemetry.METRIC_MERGE_RESULTS, result="merged") == 1
    assert set(_series(metrics.reader, telemetry.METRIC_MERGE_RESULTS)) == {
        (("result", "merged"),),
        (("result", "parked"),),
    }


def test_recorder_rejects_unknown_label_value(metrics: Harness) -> None:
    """An out-of-enum label raises before touching the meter - and adds no series."""
    import pipeline.telemetry as telemetry

    with pytest.raises(ValueError):
        telemetry.record_dispatch_outcome("exploded")
    with pytest.raises(ValueError):
        telemetry.record_authz_decision("story:delete_everything", "allowed")
    with pytest.raises(ValueError):
        telemetry.record_authz_decision("plan:dispatch", "maybe")
    with pytest.raises(ValueError):
        telemetry.record_merge_result("skipped")

    assert _points(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES) == []
    assert _points(metrics.reader, telemetry.METRIC_AUTHZ_DECISIONS) == []
    assert _points(metrics.reader, telemetry.METRIC_MERGE_RESULTS) == []


def test_gauges_read_only_the_published_snapshot(metrics: Harness) -> None:
    """Before a tick there is no series; collecting twice yields the same values."""
    import pipeline.telemetry as telemetry

    # No snapshot published yet: not "zero per status", but nothing at all.
    assert _points(metrics.reader, telemetry.METRIC_STORIES) == []
    assert _points(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == []

    telemetry.update_manifest_snapshot({"todo": 1, "done": 2}, 3)

    first = _series(metrics.reader, telemetry.METRIC_STORIES)
    second = _series(metrics.reader, telemetry.METRIC_STORIES)
    assert first == second
    assert first[(("status", "todo"),)] == 1
    assert first[(("status", "done"),)] == 2
    # One observation per status in the enum, including the zero counts.
    assert len(first) == len(telemetry.STORY_STATUSES)

    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 3
    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 3


def test_gauges_reflect_scratch_manifest_statuses(plan_dir, metrics: Harness, monkeypatch) -> None:
    """The real scan tick publishes the status counts and capacity in use."""
    import pipeline.telemetry as telemetry

    _write_manifest("plan-a", {"A-1": {"status": "in_progress"}})
    _write_manifest("plan-b", {"B-1": {"status": "failed"}})
    _write_manifest(
        "plan-c",
        {"C-1": {"status": "in_progress"}, "C-2": {"status": "done"}},
    )

    _tick(monkeypatch, plan_dir)

    stories = _series(metrics.reader, telemetry.METRIC_STORIES)
    assert stories[(("status", "in_progress"),)] == 2
    assert stories[(("status", "failed"),)] == 1
    assert stories[(("status", "done"),)] == 1
    assert stories[(("status", "todo"),)] == 0
    assert len(stories) == len(telemetry.STORY_STATUSES)
    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 2


def test_gauge_snapshot_updates_on_next_tick(plan_dir, metrics: Harness, monkeypatch) -> None:
    """Each tick republishes the complete snapshot; nothing is cached forever."""
    import pipeline.telemetry as telemetry

    _write_manifest("plan-a", {"A-1": {"status": "in_progress"}})
    _write_manifest("plan-b", {"B-1": {"status": "failed"}})
    _write_manifest(
        "plan-c",
        {"C-1": {"status": "in_progress"}, "C-2": {"status": "done"}},
    )
    _tick(monkeypatch, plan_dir)
    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 2

    # Rewrite the same three plans: A-1 done, B-1 and C-1 failed, C-2 done.
    _write_manifest("plan-a", {"A-1": {"status": "done"}})
    _write_manifest("plan-b", {"B-1": {"status": "failed"}})
    _write_manifest(
        "plan-c",
        {"C-1": {"status": "failed"}, "C-2": {"status": "done"}},
    )
    _tick(monkeypatch, plan_dir)

    stories = _series(metrics.reader, telemetry.METRIC_STORIES)
    assert stories[(("status", "in_progress"),)] == 0
    assert stories[(("status", "failed"),)] == 2
    assert stories[(("status", "done"),)] == 2
    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 0


def test_failed_tick_keeps_previous_snapshot(plan_dir, metrics: Harness, monkeypatch) -> None:
    """A tick that raises mid-scan leaves the last complete snapshot in place."""
    import pipeline.telemetry as telemetry
    import pipeline.watchers as watchers

    _write_manifest("plan-a", {"A-1": {"status": "in_progress"}})
    _write_manifest("plan-b", {"B-1": {"status": "failed"}})
    _write_manifest(
        "plan-c",
        {"C-1": {"status": "in_progress"}, "C-2": {"status": "done"}},
    )
    _tick(monkeypatch, plan_dir)
    before = _series(metrics.reader, telemetry.METRIC_STORIES)
    assert before[(("status", "in_progress"),)] == 2

    def _boom(*args, **kwargs):
        raise RuntimeError("scan blew up mid-tick")

    monkeypatch.setattr(watchers, "scan_done_markers", _boom)
    with pytest.raises(RuntimeError):
        _tick(monkeypatch, plan_dir)

    assert _series(metrics.reader, telemetry.METRIC_STORIES) == before
    assert _value(metrics.reader, telemetry.METRIC_CAPACITY_IN_USE) == 2


def test_dispatch_funnel_records_one_outcome_per_attempt(
    plan_dir, metrics: Harness, monkeypatch
) -> None:
    """The dispatch funnel records exactly one outcome per attempt.

    Every entry point (MCP tool, dashboard, the advance tick) goes through
    ``PipelineService.dispatch_story``, so recording there counts each attempt
    once - not once per enqueue plus once per worker callback.
    """
    import pipeline.server as server
    import pipeline.telemetry as telemetry
    from pipeline.service import PipelineService

    service = PipelineService()
    shapes = (
        ({"ok": True, "story_key": "S-1", "pid": 123}, "dispatched"),
        ({"ok": True, "skipped": "locked"}, "skipped"),
        ({"ok": True, "pending": "test_author", "story_key": "S-1"}, "pending"),
        ({"ok": False, "error": "no such story"}, "failed"),
    )
    for result, outcome in shapes:
        monkeypatch.setattr(server, "_dispatch_story_impl", lambda *a, _r=result: dict(_r))
        service.dispatch_story(PLAN, "S-1")
        assert (
            _value(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES, outcome=outcome)
            == 1
        ), f"{outcome} must be recorded exactly once for one attempt"

    # An exception is a failed dispatch, still recorded once.
    def _boom(*args, **kwargs):
        raise RuntimeError("worktree add failed")

    monkeypatch.setattr(server, "_dispatch_story_impl", _boom)
    assert service.dispatch_story(PLAN, "S-1")["ok"] is False
    assert (
        _value(metrics.reader, telemetry.METRIC_DISPATCH_OUTCOMES, outcome="failed") == 2
    )


def test_merge_adjudication_records_its_result(plan_dir, metrics: Harness, monkeypatch) -> None:
    """A parked adjudication records ``parked``; nothing else is touched."""
    import pipeline.advance as adv
    import pipeline.telemetry as telemetry
    from tests.unit._pipeline_mcp_server_test_helpers import _write_manifest

    monkeypatch.setattr(
        adv, "_merge_decision", lambda story: {"action": "park", "reason": "not approved"}
    )
    monkeypatch.setattr(adv, "_notify_user", lambda *a, **kw: None)
    monkeypatch.setattr(
        adv,
        "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data)),
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {
            "S-1": {
                "summary": "pr open",
                "status": "pr_open",
                "worktree": "/nonexistent-ot02-worktree",
                "dependencies": [],
            }
        },
    )

    summary = {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}
    adv._adjudicate_merges(PLAN, summary)

    assert summary["parked"] == ["S-1"]
    assert _value(metrics.reader, telemetry.METRIC_MERGE_RESULTS, result="parked") == 1
    assert set(_series(metrics.reader, telemetry.METRIC_MERGE_RESULTS)) == {
        (("result", "parked"),),
    }
