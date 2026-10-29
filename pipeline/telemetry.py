"""Opt-in OpenTelemetry tracing and metrics for the pipeline.

Both signals share one switch: ``PIPELINE_OTEL_ENDPOINT``. With it unset this
module is a no-op and never imports ``opentelemetry`` -- the SDK is imported
lazily inside :func:`configure` and :func:`configure_metrics`, so a deployment
that exports nothing pays no import cost.

Tracing
-------
Spans are exported over OTLP/HTTP under the resource
``service.name=fagan-pipeline``. Two kinds of span are produced:

* one per dashboard ``/api/`` request (:data:`REQUEST_SPAN_NAME`), carrying
  the HTTP method, the *route template*, the response status and the
  correlation id -- never a query string, header, or body;
* one per pipeline lifecycle event (:data:`LIFECYCLE_SPAN_NAME`), fed by the
  telemetry sink the event bus subscribes when tracing is enabled, carrying
  plan, story key, event type, outcome and backend/provider name -- never a
  prompt, brief or model output.

Metrics
-------
Three counters and two observable gauges describe what an operator alerts on:

* :data:`METRIC_DISPATCH_OUTCOMES` -- one record per dispatch attempt;
* :data:`METRIC_AUTHZ_DECISIONS` -- one record per authorization decision,
  labelled by action and outcome only. The principal is deliberately absent:
  it is PII and its cardinality is unbounded;
* :data:`METRIC_MERGE_RESULTS` -- one record per adjudicated story;
* :data:`METRIC_STORIES` / :data:`METRIC_CAPACITY_IN_USE` -- gauges read from a
  snapshot the scheduler tick publishes once per scan, so a collection never
  touches the filesystem.

Every label value comes from a fixed enum (see the ``*_OUTCOMES``,
``AUTHZ_ACTIONS``, ``MERGE_RESULTS`` and ``STORY_STATUSES`` sets), which is what
keeps the series count bounded; a value outside its enum raises ``ValueError``
before the meter is touched.

Export failures are swallowed: a span processor or reader that raised would
break the request or the tick it observes, so :class:`_GuardedExporter`
downgrades an exporter error to a WARNING naming the error's *type* (never its
message, which can embed request data) at most once per minute.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .events import EVENT_TYPES

logger = logging.getLogger(__name__)

#: Environment variable that turns tracing and metrics on. Unset (or empty)
#: means no-op.
ENDPOINT_ENV = "PIPELINE_OTEL_ENDPOINT"

#: ``service.name`` resource attribute every exported span carries; also the
#: instrumentation scope name the metrics are created under.
SERVICE_NAME = "fagan-pipeline"

REQUEST_SPAN_NAME = "dashboard.request"
LIFECYCLE_SPAN_NAME = "pipeline.lifecycle"

METRIC_DISPATCH_OUTCOMES = "pipeline.dispatch.outcomes"
METRIC_AUTHZ_DECISIONS = "pipeline.authz.decisions"
METRIC_MERGE_RESULTS = "pipeline.merge.results"
METRIC_STORIES = "pipeline.stories"
METRIC_CAPACITY_IN_USE = "pipeline.capacity.in_use"

#: Fixed label values for :data:`METRIC_DISPATCH_OUTCOMES`. They are exactly
#: the result shapes ``dispatch_story`` returns: a launch (``dispatched``), a
#: store-lock skip (``skipped``), a detached test-author phase
#: (``pending``) and a refusal or failure (``failed``).
DISPATCH_OUTCOMES: frozenset[str] = frozenset(
    {"dispatched", "skipped", "pending", "failed"}
)

#: Fixed label values for :data:`METRIC_AUTHZ_DECISIONS`'s ``outcome`` label;
#: the same two words ``pipeline.audit`` records for a decision.
AUTHZ_OUTCOMES: frozenset[str] = frozenset({"allowed", "denied"})

#: Fixed label values for :data:`METRIC_MERGE_RESULTS`: the three buckets the
#: advance tick files an adjudicated story under.
MERGE_RESULTS: frozenset[str] = frozenset({"merged", "parked", "failed"})

#: The story status whose count is reported as capacity in use: the one status
#: a dispatched agent holds a concurrency slot with.
CAPACITY_IN_USE_STATUS = "in_progress"

#: Event types that describe a pipeline lifecycle transition. Each one becomes
#: a span when tracing is enabled. Derived from the bus contract so a type
#: added later is traced without a second edit here; ``notification`` is the
#: one exclusion -- a delivery side effect, not a lifecycle state change.
LIFECYCLE_EVENT_TYPES: tuple[str, ...] = tuple(
    sorted(EVENT_TYPES - {"notification"})
)

#: Minimum interval between two export-failure warnings.
_WARNING_INTERVAL_SECONDS = 60.0

# Indirection so tests can drive the rate limiter without sleeping.
_monotonic = time.monotonic

_last_export_warning: float | None = None
_TRACER: Any | None = None
_PROVIDER: Any | None = None
_EXPORTER: Any | None = None

# Metrics state. ``_COUNTERS`` is keyed by metric name and stays empty while
# the switch is off, so every recorder is a no-op lookup.
_COUNTERS: dict[str, Any] = {}
_METER: Any | None = None
_METER_PROVIDER: Any | None = None

#: The last complete snapshot the scheduler tick published: ``(status_counts,
#: capacity_in_use)``. Replaced, never mutated, so a reader racing a tick sees
#: either the previous or the new snapshot in full -- never a mix of both.
_SNAPSHOT: tuple[dict[str, int], int] | None = None


def _authz_actions() -> frozenset[str]:
    """Every valid ``action`` label, from the policy's own enum."""
    from app.policy import Action

    return frozenset(action.value for action in Action)


def _story_statuses() -> frozenset[str]:
    """Every valid ``status`` label, from the set the pipeline assigns."""
    from .ci import _VALID_STORY_STATUSES

    return frozenset(_VALID_STORY_STATUSES)


def __getattr__(name: str) -> Any:
    """Expose the enum-derived label sets without importing them at load time.

    ``AUTHZ_ACTIONS`` and ``STORY_STATUSES`` read enums from ``app.policy`` and
    ``pipeline.ci``; those modules are heavy and importing them here would
    couple this module's import to theirs. Resolving on first access keeps
    ``import pipeline.telemetry`` cheap and cycle-free.
    """
    if name == "AUTHZ_ACTIONS":
        return _authz_actions()
    if name == "STORY_STATUSES":
        return _story_statuses()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def enabled() -> bool:
    """Is tracing on? True once an exporter is configured or the env var is set."""
    return _EXPORTER is not None or bool(os.environ.get(ENDPOINT_ENV))


def _log_export_failure(exc: BaseException) -> None:
    """Warn about a failed export, naming only the error type, at most 1/min."""
    global _last_export_warning
    now = _monotonic()
    if (
        _last_export_warning is not None
        and now - _last_export_warning < _WARNING_INTERVAL_SECONDS
    ):
        return
    _last_export_warning = now
    logger.warning("OpenTelemetry span export failed: %s", type(exc).__name__)


class _GuardedExporter:
    """Wrap an exporter so that no error it raises can reach the caller."""

    def __init__(self, exporter: Any) -> None:
        self._exporter = exporter

    def export(self, spans: Any) -> Any:
        try:
            return self._exporter.export(spans)
        except Exception as exc:  # noqa: BLE001 - a trace must never break the caller
            _log_export_failure(exc)
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception as exc:  # noqa: BLE001 - see export()
            _log_export_failure(exc)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        flush = getattr(self._exporter, "force_flush", None)
        if flush is None:
            return True
        try:
            return bool(flush(timeout_millis))
        except Exception as exc:  # noqa: BLE001 - see export()
            _log_export_failure(exc)
            return False


def configure(exporter: Any | None = None) -> None:
    """Install the tracer provider used by :func:`span` and friends.

    Parameters
    ----------
    exporter:
        Exporter to send spans to. ``None`` builds the OTLP/HTTP exporter from
        ``PIPELINE_OTEL_ENDPOINT``; when that variable is unset the call is a
        no-op and ``opentelemetry`` is never imported.
    """
    global _TRACER, _PROVIDER, _EXPORTER
    if _TRACER is not None:
        return
    batch = exporter is None
    if exporter is None:
        endpoint = os.environ.get(ENDPOINT_ENV)
        if not endpoint:
            return
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=endpoint)

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    guarded = _GuardedExporter(exporter)
    if batch:
        # The OTLP/HTTP exporter retries with backoff; exporting on the span's
        # own thread would stall the request or tick being traced. Batching
        # moves both the retry and any failure off the caller's path.
        provider.add_span_processor(BatchSpanProcessor(guarded))
    else:
        # An explicitly supplied exporter (tests, a host process) exports
        # synchronously so a span is observable as soon as it ends.
        provider.add_span_processor(SimpleSpanProcessor(guarded))
    _PROVIDER = provider
    _EXPORTER = exporter
    _TRACER = provider.get_tracer(SERVICE_NAME)


def _tracer() -> Any | None:
    """The tracer, lazily configured from the environment; None when disabled."""
    if _TRACER is None and os.environ.get(ENDPOINT_ENV):
        configure()
    return _TRACER


def reset() -> None:
    """Drop the tracer/exporter state (used by tests and on shutdown)."""
    global _TRACER, _PROVIDER, _EXPORTER, _last_export_warning
    provider, _PROVIDER = _PROVIDER, None
    _TRACER = None
    _EXPORTER = None
    _last_export_warning = None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception as exc:  # noqa: BLE001 - teardown never raises
            _log_export_failure(exc)


def _set_attributes(target: Any, attributes: dict[str, Any]) -> None:
    """Copy non-None attributes onto a span, stringifying non-primitives."""
    for key, value in attributes.items():
        if value is None:
            continue
        primitive = isinstance(value, (str, int, float, bool))
        target.set_attribute(key, value if primitive else str(value))


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Start a span named *name* for the duration of the block.

    Yields ``None`` (and costs nothing) when tracing is disabled.
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        _set_attributes(current, attributes or {})
        yield current


class _RequestSpan:
    """Handle for the span covering one inbound dashboard request."""

    __slots__ = ("_request", "_span")

    def __init__(self, span_: Any | None, request: Any) -> None:
        self._span = span_
        self._request = request

    def finish(self, status_code: int | None = None) -> None:
        """Record the outcome and close the span; a second call is a no-op."""
        span_, self._span = self._span, None
        if span_ is None:
            return
        try:
            route = self._request.scope.get("route")
            template = getattr(route, "path", None)
            # The route template (``/api/plans/{plan_name}/...``) is the only
            # path-like value recorded: it carries no caller-supplied id.
            if template:
                span_.set_attribute("http.route", template)
            if status_code is not None:
                span_.set_attribute("http.response.status_code", status_code)
            span_.end()
        except Exception as exc:  # noqa: BLE001 - tracing never fails a request
            _log_export_failure(exc)


def start_request_span(request: Any, correlation_id: str | None = None) -> _RequestSpan:
    """Open the span for a dashboard request; caller closes it via ``finish``."""
    tracer = _tracer()
    span_: Any | None = None
    if tracer is not None:
        try:
            span_ = tracer.start_span(REQUEST_SPAN_NAME)
            span_.set_attribute("http.request.method", request.method)
            if correlation_id:
                span_.set_attribute("correlation_id", correlation_id)
        except Exception as exc:  # noqa: BLE001 - tracing never fails a request
            _log_export_failure(exc)
            span_ = None
    return _RequestSpan(span_, request)


def record_lifecycle_event(event: dict) -> None:
    """Emit one span for a lifecycle *event* published on the bus.

    Only the event's routing keys and its outcome/backend payload fields are
    recorded: prompts, briefs and model output never reach a span.
    """
    tracer = _tracer()
    if tracer is None:
        return
    plan = event.get("plan")
    event_type = event.get("type")
    if not plan or not event_type:
        return
    payload = event.get("payload") or {}
    backend = payload.get("backend") or payload.get("provider")
    provider = payload.get("provider") or payload.get("backend")
    with span(
        LIFECYCLE_SPAN_NAME,
        {
            "pipeline.plan": plan,
            "pipeline.story_key": event.get("story_key"),
            "pipeline.event_type": event_type,
            "pipeline.outcome": payload.get("outcome"),
            "pipeline.backend": backend,
            "pipeline.provider": provider,
            "correlation_id": event.get("correlation_id"),
        },
    ):
        pass


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def configure_metrics(provider: Any | None = None) -> bool:
    """Create the counters and gauges once; return whether they are registered.

    Returns ``False`` -- and registers nothing, not even on a supplied
    ``provider`` -- when ``PIPELINE_OTEL_ENDPOINT`` is unset, so the disabled
    path exports nothing and imports no ``opentelemetry`` module.

    Parameters
    ----------
    provider:
        Meter provider to create the instruments on. ``None`` builds a
        ``MeterProvider`` exporting over OTLP/HTTP to
        ``PIPELINE_OTEL_ENDPOINT``. A second call is a no-op: repeated
        registration would duplicate every series.
    """
    global _METER, _METER_PROVIDER
    endpoint = os.environ.get(ENDPOINT_ENV)
    if not endpoint:
        return False
    if _METER is not None:
        return True

    if provider is None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        provider = MeterProvider(
            resource=Resource.create({"service.name": SERVICE_NAME}),
            metric_readers=[
                PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
            ],
        )

    meter = provider.get_meter(SERVICE_NAME)
    _COUNTERS[METRIC_DISPATCH_OUTCOMES] = meter.create_counter(
        METRIC_DISPATCH_OUTCOMES,
        unit="1",
        description="Dispatch attempts by outcome.",
    )
    _COUNTERS[METRIC_AUTHZ_DECISIONS] = meter.create_counter(
        METRIC_AUTHZ_DECISIONS,
        unit="1",
        description="Authorization decisions by action and outcome.",
    )
    _COUNTERS[METRIC_MERGE_RESULTS] = meter.create_counter(
        METRIC_MERGE_RESULTS,
        unit="1",
        description="Merge adjudications by result.",
    )
    meter.create_observable_gauge(
        METRIC_STORIES,
        callbacks=[_observe_stories],
        unit="1",
        description="Stories in the loaded manifests by status.",
    )
    meter.create_observable_gauge(
        METRIC_CAPACITY_IN_USE,
        callbacks=[_observe_capacity_in_use],
        unit="1",
        description="Dispatched agents holding a concurrency slot.",
    )
    _METER = meter
    _METER_PROVIDER = provider
    return True


def reset_metrics() -> None:
    """Forget the instruments and the published snapshot (tests, shutdown).

    The meter provider itself is left alone: its lifecycle belongs to whoever
    built it (the module for the OTLP provider, the test for an in-memory one).
    """
    global _METER, _METER_PROVIDER, _SNAPSHOT
    _COUNTERS.clear()
    _METER = None
    _METER_PROVIDER = None
    _SNAPSHOT = None


def _checked_label(value: Any, allowed: frozenset[str], what: str) -> str:
    """Return *value* as a label string, or raise when it is outside *allowed*.

    Validation runs before the meter is touched so an out-of-enum value can
    never create a new series.
    """
    label = value.value if hasattr(value, "value") else str(value)
    if label not in allowed:
        raise ValueError(f"invalid {what} {label!r}: must be one of {sorted(allowed)}")
    return label


def _add(metric_name: str, attributes: dict[str, str]) -> None:
    """Add one to *metric_name*, or do nothing while metrics are disabled."""
    counter = _COUNTERS.get(metric_name)
    if counter is None:
        return
    counter.add(1, attributes)


def dispatch_outcome(result: Any) -> str:
    """Classify a dispatch result into one of :data:`DISPATCH_OUTCOMES`.

    The dispatch funnel returns one of four shapes -- a launch, a skip, a
    pending test-author phase, or a failure -- and this is the single place
    that shape becomes a label. Keeping the mapping here, next to the enum it
    feeds, means the funnel itself only has to hand over its result.
    """
    if not isinstance(result, dict):
        return "dispatched"
    if result.get("ok") is False:
        return "failed"
    if result.get("skipped"):
        return "skipped"
    if result.get("pending"):
        return "pending"
    return "dispatched"


def record_dispatch_outcome(outcome: str) -> None:
    """Record one dispatch attempt under *outcome* (:data:`DISPATCH_OUTCOMES`)."""
    _add(
        METRIC_DISPATCH_OUTCOMES,
        {"outcome": _checked_label(outcome, DISPATCH_OUTCOMES, "dispatch outcome")},
    )


def record_dispatch_attempt(result: Any) -> None:
    """Record exactly one outcome for one dispatch attempt.

    Call once per attempt with the funnel's result, including the shape a
    failure is reported with (``{"ok": False}``); recording in both an enqueue
    path and a worker callback would double-count the same attempt.
    """
    record_dispatch_outcome(dispatch_outcome(result))


def record_authz_decision(action: Any, outcome: str) -> None:
    """Record one authorization decision by *action* and *outcome*.

    The principal is never a label: it is PII and its cardinality is unbounded.
    """
    _add(
        METRIC_AUTHZ_DECISIONS,
        {
            "action": _checked_label(action, _authz_actions(), "authorization action"),
            "outcome": _checked_label(outcome, AUTHZ_OUTCOMES, "authorization outcome"),
        },
    )


def record_merge_result(result: str) -> None:
    """Record one adjudicated story under *result* (:data:`MERGE_RESULTS`)."""
    _add(
        METRIC_MERGE_RESULTS,
        {"result": _checked_label(result, MERGE_RESULTS, "merge result")},
    )


def update_manifest_snapshot(
    status_counts: dict[str, int], capacity_in_use: int
) -> None:
    """Publish the manifest-derived snapshot the gauges read on collection.

    Called once per successful scheduler tick with counts the tick already
    computed from the manifests it loaded, so a collection never touches the
    filesystem. Statuses outside :data:`STORY_STATUSES` are dropped: the labels
    stay bounded to the enum, and a manifest carrying a retired status must not
    break the tick. The new snapshot is built in locals and published with one
    assignment, so a reader never observes a half-written snapshot.
    """
    global _SNAPSHOT
    statuses = _story_statuses()
    counts = {
        status: max(0, int(status_counts.get(status, 0))) for status in statuses
    }
    _SNAPSHOT = (counts, max(0, int(capacity_in_use)))


def _observe_stories(options: Any = None) -> Iterator[Any]:
    """Gauge callback: one observation per status, from the snapshot only."""
    from opentelemetry.metrics import Observation

    snapshot = _SNAPSHOT
    if snapshot is None:
        return
    for status, count in snapshot[0].items():
        yield Observation(count, {"status": status})


def _observe_capacity_in_use(options: Any = None) -> Iterator[Any]:
    """Gauge callback: the capacity in use from the snapshot only."""
    from opentelemetry.metrics import Observation

    snapshot = _SNAPSHOT
    if snapshot is None:
        return
    yield Observation(snapshot[1], {})


def flush(timeout_millis: int = 5000) -> None:
    """Force-flush and shut down any providers this module built.

    A short-lived process (one plan advance) exits as soon as its work is done,
    so spans and metrics still buffered in a batch exporter would be dropped.
    Flushing and shutting the providers down here makes the export happen
    before the interpreter goes away.

    No-op when nothing is configured (``PIPELINE_OTEL_ENDPOINT`` unset), and
    never raises: telemetry must not be able to fail a run, so a provider error
    is logged by exception class only (the message can carry paths). The module
    globals keep their providers so a second call flushes just as effectively.
    """
    for provider in (_PROVIDER, _METER_PROVIDER):
        if provider is None:
            continue
        _flush_provider(provider, timeout_millis)


def _flush_provider(provider: Any, timeout_millis: int) -> None:
    """Force-flush then shut down one provider, isolating their failures."""
    for call in (lambda: provider.force_flush(timeout_millis), provider.shutdown):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a run
            logger.warning("telemetry flush failed: %s", type(exc).__name__)
