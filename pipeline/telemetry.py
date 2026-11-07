"""Opt-in OpenTelemetry tracing and metrics for the pipeline.

Both signals share one switch: an OTLP endpoint. It is set either by the
standard OpenTelemetry variables (``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` /
``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``, or the shared
``OTEL_EXPORTER_OTLP_ENDPOINT``) or by the deprecated ``PIPELINE_OTEL_ENDPOINT``
alias. When no OTLP endpoint is configured this
module is a no-op and never imports ``opentelemetry`` -- the SDK is imported
lazily inside :func:`configure` and :func:`configure_metrics`, so a deployment
that exports nothing pays no import cost.

Tracing
-------
Spans are exported over OTLP/HTTP under the resource ``service.name``, which
is ``fagan`` unless ``OTEL_SERVICE_NAME`` overrides it. Two kinds of span are
produced:

* one per dashboard ``/api/`` request (:data:`REQUEST_SPAN_NAME`), carrying
  the HTTP method, the *route template*, the response status and the
  correlation id -- never a query string, header, or body;
* one per pipeline lifecycle event (:data:`LIFECYCLE_SPAN_NAME`), fed by the
  telemetry sink the event bus subscribes when tracing or metrics is enabled, carrying
  plan, story key, event type, outcome and backend/provider name -- never a
  prompt, brief or model output. The events of one dispatch attempt share one
  trace, derived from the story's correlation id and the attempt number, and an
  attempt after the first links back to the previous attempt's root span.

Metrics
-------
Three counters, two observable gauges and three label-free duration
histograms describe what an operator alerts on:

* :data:`METRIC_DISPATCH_OUTCOMES` -- one record per dispatch attempt;
* :data:`METRIC_AUTHZ_DECISIONS` -- one record per authorization decision,
  labelled by action and outcome only. The principal is deliberately absent:
  it is PII and its cardinality is unbounded;
* :data:`METRIC_MERGE_RESULTS` -- one record per adjudicated story;
* :data:`METRIC_STORIES` / :data:`METRIC_CAPACITY_IN_USE` -- gauges read from a
  snapshot the scheduler tick publishes once per scan, so a collection never
  touches the filesystem;
* :data:`pipeline.telemetry_durations.DISPATCH_TO_PR`,
  :data:`~pipeline.telemetry_durations.PR_TO_CI` and
  :data:`~pipeline.telemetry_durations.PR_TO_DONE` -- label-free seconds spent
  in each hop of a story's life, recorded from the same lifecycle sink.

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
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote

from . import telemetry_durations, telemetry_profile, telemetry_usage
from .events import EVENT_TYPES
from .trace_context import ATTEMPT_ROOT_SPAN_ID, attempt_trace_id, parse_traceparent

logger = logging.getLogger(__name__)

#: Deprecated alias that turns tracing and metrics on. Unset (or empty) means no-op.
#: ``_signal_endpoint`` prefers the standard OpenTelemetry endpoint variables.
ENDPOINT_ENV = "PIPELINE_OTEL_ENDPOINT"

#: Standard OpenTelemetry endpoint variables. A set standard variable wins over the alias
#: and is read by the OTLP exporter itself, so headers, certificates and timeouts
#: (OTEL_EXPORTER_OTLP_HEADERS, _CERTIFICATE, _TIMEOUT) apply with no code here.
_TRACES_ENDPOINT_ENVS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
_METRICS_ENDPOINT_ENVS = ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")


def _signal_endpoint(standard_envs: tuple[str, ...]) -> tuple[bool, str | None]:
    """(configured, explicit endpoint) for one signal.

    A set standard variable means configured with no explicit endpoint (the exporter reads
    the environment itself); otherwise the deprecated alias means configured with that URL.
    """
    if any(os.environ.get(name) for name in standard_envs):
        return True, None
    alias = os.environ.get(ENDPOINT_ENV)
    return (True, alias) if alias else (False, None)


#: One identifier per process, so every signal's resource names the same instance.
_INSTANCE_ID = uuid.uuid4().hex

#: Correlation ids are opaque routing keys, so only their shape is checked before
#: one feeds the trace-id derivation; anything else keeps today's root-span path.
_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: ``(plan, story_key) -> attempt`` for the attempt currently being traced. The
#: attempt number rides only on ``agent_dispatched``, so it is remembered here and
#: applied to the events that follow. Bounded and lossy on purpose: telemetry is
#: lossy by contract, and a sink must never grow with the number of stories.
_ATTEMPTS: OrderedDict[tuple[str, str], int] = OrderedDict()
_ATTEMPTS_LOCK = threading.Lock()
_ATTEMPTS_MAX = 512

#: Namespace core reports when the deployment supplies none. Core gives it no meaning (ADR 0012).
DEFAULT_NAMESPACE = "default"


def _resource_attributes() -> dict[str, str]:
    """Resource attributes: Fagan defaults, overridden by the standard environment.

    OTEL_RESOURCE_ATTRIBUTES (comma-separated key=value, values percent-decoded) and
    OTEL_SERVICE_NAME override the defaults. The namespace comes only from this environment
    or the default, never from request data.
    """
    attributes = {
        "service.name": SERVICE_NAME,
        "service.instance.id": _INSTANCE_ID,
        "fagan.namespace": DEFAULT_NAMESPACE,
    }
    for pair in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            attributes[key.strip()] = unquote(value.strip())
    service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip()
    if service_name:
        attributes["service.name"] = service_name
    return attributes


#: Default ``service.name`` resource attribute (``OTEL_SERVICE_NAME`` overrides
#: it on the exported resource); also the instrumentation scope name the metrics
#: are created under.
SERVICE_NAME = "fagan"

REQUEST_SPAN_NAME = "fagan.dashboard.request"
LIFECYCLE_SPAN_NAME = "fagan.lifecycle"

METRIC_DISPATCH_OUTCOMES = "fagan.dispatch.outcomes"
METRIC_AUTHZ_DECISIONS = "fagan.authz.decisions"
METRIC_MERGE_RESULTS = "fagan.merge.results"
METRIC_STORIES = "fagan.stories"
METRIC_CAPACITY_IN_USE = "fagan.capacity.in_use"

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
    """Is tracing on? True once an exporter is configured or an OTLP endpoint variable is set."""
    return _EXPORTER is not None or _signal_endpoint(_TRACES_ENDPOINT_ENVS)[0]


def metrics_enabled() -> bool:
    """Are metrics on? True once configured or an OTLP metrics endpoint is set."""
    return _METER is not None or _signal_endpoint(_METRICS_ENDPOINT_ENVS)[0]


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
        an OTLP endpoint read from the environment; when no OTLP endpoint is
        configured the call is a no-op and ``opentelemetry`` is never imported.
    """
    global _TRACER, _PROVIDER, _EXPORTER
    if _TRACER is not None:
        return
    batch = exporter is None
    if exporter is None:
        configured, endpoint = _signal_endpoint(_TRACES_ENDPOINT_ENVS)
        if not configured:
            return
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create(_resource_attributes()))
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
    if _TRACER is None and _signal_endpoint(_TRACES_ENDPOINT_ENVS)[0]:
        configure()
    return _TRACER


def reset() -> None:
    """Drop the tracer/exporter state and the remembered attempts (tests, shutdown)."""
    global _TRACER, _PROVIDER, _EXPORTER, _last_export_warning
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.clear()
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
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    context: Any | None = None,
    links: Any | None = None,
) -> Iterator[Any]:
    """Start a span named *name* for the duration of the block.

    Yields ``None`` (and costs nothing) when tracing is disabled. *context* (a
    parent ``Context``) and *links* are forwarded to the tracer; both default to
    ``None``, which is exactly what omitting them means.
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(
        name, context=context, links=links
    ) as current:
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


def _traceparent_parent_context(request: Any) -> Any | None:
    """Return a Context carrying the caller's remote parent span, or None.

    The traceparent header is caller-controlled, so it is accepted only when
    pipeline.trace_context.parse_traceparent validates it. tracestate is never
    propagated, and the raw header value is never logged.
    """
    header = request.headers.get("traceparent")
    if header is None:
        return None
    parsed = parse_traceparent(header)
    if parsed is None:
        return None
    from opentelemetry import trace

    parent = trace.SpanContext(
        trace_id=int(parsed["trace_id"], 16),
        span_id=int(parsed["span_id"], 16),
        is_remote=True,
        trace_flags=trace.TraceFlags(int(parsed["flags"], 16)),
    )
    return trace.set_span_in_context(trace.NonRecordingSpan(parent))


def start_request_span(request: Any, correlation_id: str | None = None) -> _RequestSpan:
    """Open the span for a dashboard request; caller closes it via ``finish``.

    A valid inbound ``traceparent`` becomes the span's remote parent, so the
    caller's trace continues instead of forking. The sampled flag of the inbound
    context is honoured by the SDK's parent-based sampler; Fagan does not
    override it.
    """
    tracer = _tracer()
    span_: Any | None = None
    if tracer is not None:
        try:
            parent_context = _traceparent_parent_context(request)
            span_ = tracer.start_span(REQUEST_SPAN_NAME, context=parent_context)
            span_.set_attribute("http.request.method", request.method)
            if correlation_id:
                span_.set_attribute("correlation_id", correlation_id)
        except Exception as exc:  # noqa: BLE001 - tracing never fails a request
            _log_export_failure(exc)
            span_ = None
    return _RequestSpan(span_, request)


def _attempt_span_kwargs(
    plan: Any,
    story_key: Any,
    correlation_id: Any,
    event_type: Any,
    payload: dict,
) -> tuple[Any | None, Any | None]:
    """Parent context and links that scope one lifecycle span to its attempt.

    The attempt number is carried only by ``agent_dispatched``, so it is
    remembered per ``(plan, story_key)`` for the events that follow, and the
    trace id is derived from ``(correlation_id, attempt)``: every event of one
    attempt lands in one trace, and a new attempt opens a new one. Attempt 1 and
    later link back to the previous attempt's root span.

    An event arriving in a process that never saw the attempt's
    ``agent_dispatched`` is filed under attempt 0; that is acceptable loss for
    the telemetry plane.

    Returns ``(None, None)`` -- today's root-span behaviour -- when there is no
    usable correlation id. Any failure here degrades to the same pair, so the
    bus subscriber can never raise.
    """
    try:
        from opentelemetry import trace

        attempt = payload.get("attempt")
        if event_type == "agent_dispatched" and isinstance(attempt, int) and attempt >= 0:
            with _ATTEMPTS_LOCK:
                _ATTEMPTS[(plan, story_key)] = attempt
                # Re-dispatching an existing story refreshes its recency, so the
                # bound evicts the story that has been quiet longest.
                _ATTEMPTS.move_to_end((plan, story_key))
                while len(_ATTEMPTS) > _ATTEMPTS_MAX:
                    _ATTEMPTS.popitem(last=False)
        with _ATTEMPTS_LOCK:
            attempt = _ATTEMPTS.get((plan, story_key), 0)
        if not isinstance(correlation_id, str) or not _CORRELATION_ID_RE.fullmatch(
            correlation_id
        ):
            return None, None
        # A sampled parent: the process-wide default sampler is parent-based, so
        # an unsampled remote parent would drop every child span of the attempt
        # and this plane would export nothing at all.
        parent = trace.SpanContext(
            trace_id=int(attempt_trace_id(correlation_id, attempt), 16),
            span_id=int(ATTEMPT_ROOT_SPAN_ID, 16),
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
        links = None
        if attempt > 0:
            previous = trace.SpanContext(
                trace_id=int(attempt_trace_id(correlation_id, attempt - 1), 16),
                span_id=int(ATTEMPT_ROOT_SPAN_ID, 16),
                is_remote=True,
                trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
            )
            links = [trace.Link(previous)]
        return trace.set_span_in_context(trace.NonRecordingSpan(parent)), links
    except Exception as exc:  # noqa: BLE001 - tracing never breaks the bus
        _log_export_failure(exc)
        return None, None


def record_lifecycle_event(event: dict) -> None:
    """Emit one span and record one duration sample for a lifecycle *event*.

    Only the event's routing keys and its outcome/backend payload fields are
    recorded: prompts, briefs and model output never reach a span or a metric.
    """
    telemetry_durations.observe(event)
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
    story_key = event.get("story_key")
    correlation_id = event.get("correlation_id")
    parent_context, links = _attempt_span_kwargs(
        plan, story_key, correlation_id, event_type, payload
    )
    harness_attributes: dict[str, Any] = {}
    if telemetry_profile.is_harness() and event_type == "agent_dispatched":
        role, model = payload.get("role"), payload.get("model")
        attempt = payload.get("attempt")
        if isinstance(role, str):
            harness_attributes["fagan.role"] = role
        if isinstance(model, str):
            harness_attributes["fagan.model"] = model
        if isinstance(attempt, int) and not isinstance(attempt, bool) and 0 <= attempt:
            harness_attributes["fagan.attempt"] = attempt
    with span(
        LIFECYCLE_SPAN_NAME,
        {
            "fagan.plan": plan,
            "fagan.story_key": event.get("story_key"),
            "fagan.event_type": event_type,
            "fagan.outcome": payload.get("outcome"),
            "fagan.backend": backend,
            "fagan.provider": provider,
            "correlation_id": event.get("correlation_id"),
            **harness_attributes,
        },
        context=parent_context,
        links=links,
    ):
        pass


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def configure_metrics(provider: Any | None = None) -> bool:
    """Create the counters and gauges once; return whether they are registered.

    Returns ``False`` -- and registers nothing, not even on a supplied
    ``provider`` -- when no OTLP endpoint is configured, so the disabled
    path exports nothing and imports no ``opentelemetry`` module.

    Parameters
    ----------
    provider:
        Meter provider to create the instruments on. ``None`` builds a
        ``MeterProvider`` exporting over OTLP/HTTP to
        an OTLP endpoint read from the environment. A second call is a no-op:
        repeated registration would duplicate every series.
    """
    global _METER, _METER_PROVIDER
    configured, endpoint = _signal_endpoint(_METRICS_ENDPOINT_ENVS)
    if not configured:
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
            resource=Resource.create(_resource_attributes()),
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint)
                    if endpoint
                    else OTLPMetricExporter()
                )
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
    telemetry_durations.register(meter)
    if telemetry_profile.is_harness():
        telemetry_usage.register(meter)
    return True


def reset_metrics() -> None:
    """Forget the instruments and the published snapshot (tests, shutdown).

    The meter provider itself is left alone: its lifecycle belongs to whoever
    built it (the module for the OTLP provider, the test for an in-memory one).
    """
    global _METER, _METER_PROVIDER, _SNAPSHOT
    _COUNTERS.clear()
    telemetry_durations.reset()
    telemetry_usage.reset()
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

    No-op when no OTLP endpoint is configured, and
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
