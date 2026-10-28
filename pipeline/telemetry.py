"""Opt-in OpenTelemetry tracing for dashboard requests and the pipeline lifecycle.

Tracing is OFF unless ``PIPELINE_OTEL_ENDPOINT`` configuration is
supplied: with ``PIPELINE_OTEL_ENDPOINT`` unset this module is a no-op and
never imports ``opentelemetry`` -- the SDK is imported lazily inside
:func:`configure` and :func:`_tracer`, so a deployment that does not trace
pays no import cost.

With the endpoint set, spans are exported over OTLP/HTTP under the resource
``service.name=fagan-pipeline``. Two kinds of span are produced:

* one per dashboard ``/api/`` request (:data:`REQUEST_SPAN_NAME`), carrying
  the HTTP method, the *route template*, the response status and the
  correlation id -- never a query string, header, or body;
* one per pipeline lifecycle event (:data:`LIFECYCLE_SPAN_NAME`), fed by the
  telemetry sink the event bus subscribes when tracing is enabled, carrying
  plan, story key, event type, outcome and backend/provider name -- never a
  prompt, brief or model output.

Export failures are swallowed: a span processor that raised would break the
request or the tick it observes, so :class:`_GuardedExporter` downgrades an
exporter error to a WARNING naming the error's *type* (never its message,
which can embed request data) at most once per minute.
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

#: Environment variable that turns tracing on. Unset (or empty) means no-op.
ENDPOINT_ENV = "PIPELINE_OTEL_ENDPOINT"

#: ``service.name`` resource attribute every exported span carries.
SERVICE_NAME = "fagan-pipeline"

REQUEST_SPAN_NAME = "dashboard.request"
LIFECYCLE_SPAN_NAME = "pipeline.lifecycle"

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
