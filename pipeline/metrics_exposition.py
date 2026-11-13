"""Prometheus text exposition (format 0.0.4) for the pipeline's liveness signals.

This module is pure: it imports no web framework and returns strings, so the
same snapshot can be asserted byte-for-byte in a unit test. Two functions:

* :func:`render` turns a snapshot into the exposition format -- a ``# HELP``
  line, a ``# TYPE`` line and then the samples for each metric, in the fixed
  order of :data:`METRICS`, with label values escaped and a trailing newline.
  A sample whose value is ``None`` (unknown, rather than zero) is omitted: an
  exposition must never invent a ``NaN`` for "we could not tell".
* :func:`collect` gathers the snapshot from the producers the dashboard
  already reads -- :func:`pipeline.health_freshness.health_extras`,
  the record store's outbox aggregate (via :func:`outbox_summary` here),
  :func:`pipeline.events_nats_consumer.consumer_status` and
  :func:`pipeline.service_levels.service_level_percentiles` -- plus the plans'
  approval status, counted from the injected store's record view. It takes the
  host state and the store as arguments, defaulting to
  :func:`pipeline.host_state.build_host_state` and
  :func:`pipeline.store.build_store`. It NEVER raises: a producer that fails omits its metric and increments
  ``fagan_metrics_errors_total{source}``.

Secure by Design: every label value comes from a closed set (an approval
status, an error-source name) or from a broker durable name
(``pipeline-<event_type>``). No plan name, story key, file path, subject,
e-mail address or exception text is ever rendered -- telemetry is readable by
anything holding a scrape credential, so it carries aggregates only.
"""
from __future__ import annotations

import logging
from typing import Any

from pipeline import parsers
from pipeline.component_health import component_status
from pipeline.events_nats_consumer import consumer_status
from pipeline.health_freshness import health_extras
from pipeline.service_levels import MEASURES as SERVICE_LEVEL_MEASURES
from pipeline.service_levels import service_level_percentiles
from pipeline.store_records import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)

#: The plan approval statuses, in the fixed exposition order. ``none`` covers a
#: manifest with no approval record as well as an unrecognised one, so the
#: label value stays in this closed set whatever a manifest holds.
PLAN_STATUSES = ("approved", "pending_approval", "rejected", "none")

#: The producers whose failure is counted, as the ``source`` label values.
SOURCES = (
    "health_extras",
    "outbox_summary",
    "consumer_status",
    "plans",
    "service_levels",
)

#: The quantile label values the service-level gauges carry, with the key each
#: one reads from a measure's payload.
QUANTILES: tuple[tuple[str, str], ...] = (("0.5", "p50"), ("0.9", "p90"))

#: The complete metric set, in the one order :func:`render` emits it:
#: ``(name, type, help)``. Every name is ``fagan_``-prefixed.
METRICS: tuple[tuple[str, str, str], ...] = (
    (
        "fagan_scheduler_age_seconds",
        "gauge",
        "Age in seconds of the scheduler's last health snapshot.",
    ),
    (
        "fagan_scheduler_healthy",
        "gauge",
        "Whether the scheduler health probe reports healthy (1) or not (0).",
    ),
    (
        "fagan_outbox_records",
        "gauge",
        "Number of records waiting in the notification outbox.",
    ),
    (
        "fagan_outbox_oldest_age_seconds",
        "gauge",
        "Age in seconds of the oldest notification outbox record.",
    ),
    (
        "fagan_broker_reachable",
        "gauge",
        "Whether the NATS broker answered the consumer status probe (1) or not (0).",
    ),
    (
        "fagan_consumer_pending",
        "gauge",
        "Messages pending for the durable consumer.",
    ),
    (
        "fagan_consumer_subscribed",
        "gauge",
        "Whether the durable consumer is subscribed (1) or not (0).",
    ),
    (
        "fagan_consumer_dead_lettered",
        "gauge",
        "Messages on the durable consumer's dead-letter subject.",
    ),
    (
        "fagan_consumer_oldest_pending_age_seconds",
        "gauge",
        "Age in seconds of the oldest pending message for the durable consumer.",
    ),
    (
        "fagan_plans",
        "gauge",
        "Number of plan manifests by approval status.",
    ),
    (
        "fagan_story_dispatch_to_review_seconds",
        "gauge",
        "Seconds from a story's first dispatch to the start of its first review.",
    ),
    (
        "fagan_story_review_to_merge_seconds",
        "gauge",
        "Seconds from a story's first review start to its merge, rework included.",
    ),
    (
        "fagan_story_cycle_seconds",
        "gauge",
        "Seconds from a story's first dispatch to its merge.",
    ),
    (
        "fagan_story_service_level_samples",
        "gauge",
        "Stories behind each service-level percentile in the window.",
    ),
    (
        "fagan_metrics_errors_total",
        "counter",
        "Producer failures while collecting the metrics snapshot.",
    ),
    (
        "fagan_component_up",
        "gauge",
        "Whether a pipeline component is up (1) or down (0).",
    ),
    (
        "fagan_component_health_age_seconds",
        "gauge",
        "Age in seconds of a component's last health report, when it is known.",
    ),
)


def _escape_label_value(value: Any) -> str:
    """Escape the three characters the exposition format reserves in a label."""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_block(labels: dict[str, Any]) -> str:
    """Return ``{name="value",...}`` (sorted, empty when there are no labels)."""
    if not labels:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label_value(labels[name])}"' for name in sorted(labels)
    )
    return "{" + pairs + "}"


def _format_value(value: Any) -> str:
    """Render a sample value; a bool is a gauge's 1/0, never ``True``."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def render(snapshot: dict[str, list[tuple[dict[str, Any], Any]]]) -> str:
    """Return *snapshot* as Prometheus text exposition format 0.0.4.

    *snapshot* maps a metric name to ``(labels, value)`` samples. Metrics are
    emitted in :data:`METRICS` order; a metric with no renderable sample (or a
    sample whose value is ``None``) is omitted entirely, so no ``NaN`` and no
    empty ``# HELP``/``# TYPE`` pair is ever produced.
    """
    lines: list[str] = []
    for name, metric_type, help_text in METRICS:
        samples = [
            (labels, value)
            for labels, value in snapshot.get(name) or ()
            if value is not None
        ]
        if not samples:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")
        for labels, value in samples:
            lines.append(f"{name}{_label_block(labels)} {_format_value(value)}")
    return "".join(f"{line}\n" for line in lines)


def _approval_status(manifest: Any) -> str:
    """Return *manifest*'s approval status, or ``"none"`` when it is not one."""
    if not isinstance(manifest, dict):
        return "none"
    approval = manifest.get("approval")
    status = approval.get("status") if isinstance(approval, dict) else None
    return status if status in PLAN_STATUSES else "none"


def _plan_samples(records: Any, counts: dict[str, int]) -> list[tuple[dict, int]]:
    """Count the plans the injected store knows by approval status, in fixed order.

    The plan listing and each manifest come from the store's record view, so a
    store-backed deployment counts the plans its record store knows and never
    this host's manifest files. An empty store therefore counts zero plans.

    Every name is checked by :func:`pipeline.parsers._validate_key` before it is
    read, because the record store's ``list_plans`` is not separator-checked: a
    store-supplied ``"../x"`` or a dot-only name must not reach a read. An
    invalid name is skipped: it is neither read nor counted. A manifest that is
    missing or not a JSON object is skipped rather than counted.
    """
    for name in records.list_plans(DEFAULT_NAMESPACE):
        name = str(name)
        try:
            parsers._validate_key(name)
        except ValueError:
            continue
        manifest = records.get_manifest(DEFAULT_NAMESPACE, name)
        if not isinstance(manifest, dict):
            continue
        counts[_approval_status(manifest)] += 1
    return [({"status": status}, counts[status]) for status in PLAN_STATUSES]


def _scheduler_samples(snapshot: dict, extras: Any) -> None:
    """Add the scheduler gauges present in a ``health_extras`` payload."""
    if not isinstance(extras, dict):
        return
    if extras.get("scheduler_age_s") is not None:
        snapshot["fagan_scheduler_age_seconds"] = [({}, extras["scheduler_age_s"])]
    if "healthy" in extras:
        snapshot["fagan_scheduler_healthy"] = [({}, bool(extras["healthy"]))]


def _outbox_samples(snapshot: dict, summary: Any) -> None:
    """Add the notification-outbox gauges present in an ``outbox_summary``."""
    if not isinstance(summary, dict):
        return
    if summary.get("records") is not None:
        snapshot["fagan_outbox_records"] = [({}, summary["records"])]
    if summary.get("oldest_age_s") is not None:
        snapshot["fagan_outbox_oldest_age_seconds"] = [({}, summary["oldest_age_s"])]


def _consumer_samples(snapshot: dict, status: Any) -> None:
    """Add the broker and per-durable gauges for a ``consumer_status`` answer.

    ``{"reachable": False}`` is the producer's one-word "the broker did not
    answer": it becomes ``fagan_broker_reachable 0`` and nothing per durable,
    because a pending count we could not read is unknown, not zero.
    """
    if not isinstance(status, dict) or not status:
        return
    if status.get("reachable") is False:
        snapshot["fagan_broker_reachable"] = [({}, 0)]
        return
    snapshot["fagan_broker_reachable"] = [({}, 1)]
    per_durable = (
        ("fagan_consumer_pending", "pending"),
        ("fagan_consumer_subscribed", "subscribed"),
        ("fagan_consumer_dead_lettered", "dead_lettered"),
        ("fagan_consumer_oldest_pending_age_seconds", "oldest_pending_age_s"),
    )
    for metric, field in per_durable:
        snapshot[metric] = [
            ({"durable": durable}, status[durable].get(field))
            for durable in sorted(status)
            if isinstance(status[durable], dict)
        ]


def _service_level_samples(snapshot: dict, percentiles: Any) -> None:
    """Add the service-level percentile gauges for a percentiles payload.

    Every measure's sample count is rendered, including a zero: a thin sample
    is then visible next to the percentile instead of the percentile silently
    standing alone. The quantile samples are rendered only for a measure that
    has samples at all (``n > 0``), so an unknown p90 is absent rather than 0.
    """
    if not isinstance(percentiles, dict):
        return
    counts: list[tuple[dict[str, Any], Any]] = []
    for measure in SERVICE_LEVEL_MEASURES:
        bucket = percentiles.get(measure)
        if not isinstance(bucket, dict):
            bucket = {"n": 0}
        counts.append(({"measure": measure}, bucket.get("n", 0)))
        if not bucket.get("n"):
            continue
        for label, key in QUANTILES:
            snapshot.setdefault(f"fagan_story_{measure}_seconds", []).append(
                ({"quantile": label}, bucket.get(key))
            )
    snapshot["fagan_story_service_level_samples"] = counts


def outbox_summary(store: Any) -> dict:
    """Return the notification outbox aggregate, from the injected store.

    The store owns the outbox queues, so the scrape asks it for the aggregate
    rather than walking a directory. Kept as this module's own producer seam, so
    ``collect`` calls it through the module attribute and a test can replace it.
    """
    return store.records().outbox_summary(DEFAULT_NAMESPACE)


def _component_samples(snapshot: dict, rows: Any) -> None:
    """Add the per-component gauges for a :func:`component_status` report.

    Every row becomes a ``fagan_component_up`` sample (1 up, 0 down). A row whose
    age is unknown (``None``) gets no ``fagan_component_health_age_seconds``
    sample: an unknown age must be absent, never a fabricated zero.
    """
    if not isinstance(rows, (list, tuple)):
        return
    up_samples: list[tuple[dict[str, Any], int]] = []
    age_samples: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        component = row.get("component")
        if not isinstance(component, str) or not component:
            continue
        up_samples.append(({"component": component}, 1 if row.get("up") else 0))
        age = row.get("age_s")
        if age is None:
            continue
        try:
            age_samples.append(({"component": component}, float(age)))
        except (TypeError, ValueError):
            continue
    if up_samples:
        snapshot["fagan_component_up"] = up_samples
    if age_samples:
        snapshot["fagan_component_health_age_seconds"] = age_samples


def collect(
    host_state: Any = None, store: Any = None
) -> dict[str, list[tuple[dict, Any]]]:
    """Return the metrics snapshot, never raising.

    *host_state* defaults to :func:`pipeline.host_state.build_host_state` (inside
    :func:`health_extras`) and *store* to :func:`pipeline.store.build_store`, both
    resolved lazily so this module stays importable without the full runtime
    configuration. A test passes its own and reads the same stored state the
    ``/api/health`` payload does.

    Each producer is called through this module's attribute
    (``metrics_exposition.health_extras``), which is the seam a test replaces;
    a producer that raises omits its metric and increments
    ``fagan_metrics_errors_total{source}``. The exception text is logged as a
    source name only -- a broker error can carry connection details.
    """
    from pipeline.store import build_store

    try:
        injected = build_store() if store is None else store
    except Exception:  # noqa: BLE001 - a fail-closed store must not break the scrape
        # Every store-backed producer below then fails on ``None`` and is
        # counted in ``fagan_metrics_errors_total`` like any other failure.
        logger.warning("metrics store unavailable")
        injected = None
    errors = dict.fromkeys(SOURCES, 0)
    snapshot: dict[str, list[tuple[dict, Any]]] = {}

    try:
        _scheduler_samples(snapshot, health_extras(host_state, injected))
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["health_extras"] += 1
        logger.warning("metrics producer failed", extra={"metrics_source": "health_extras"})

    try:
        _outbox_samples(snapshot, outbox_summary(injected))
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["outbox_summary"] += 1
        logger.warning("metrics producer failed", extra={"metrics_source": "outbox_summary"})

    try:
        _consumer_samples(snapshot, consumer_status())
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["consumer_status"] += 1
        logger.warning(
            "metrics producer failed", extra={"metrics_source": "consumer_status"}
        )

    try:
        snapshot["fagan_plans"] = _plan_samples(
            injected.records(), dict.fromkeys(PLAN_STATUSES, 0)
        )
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["plans"] += 1
        logger.warning("metrics producer failed", extra={"metrics_source": "plans"})

    try:
        _service_level_samples(
            snapshot, service_level_percentiles(injected.audit_log())
        )
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["service_levels"] += 1
        logger.warning(
            "metrics producer failed", extra={"metrics_source": "service_levels"}
        )

    try:
        _component_samples(snapshot, component_status())
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        logger.warning(
            "metrics producer failed", extra={"metrics_source": "component_status"}
        )

    snapshot["fagan_metrics_errors_total"] = [
        ({"source": source}, errors[source]) for source in SOURCES
    ]
    return snapshot
