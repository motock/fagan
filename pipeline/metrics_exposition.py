"""Prometheus text exposition (format 0.0.4) for the pipeline's liveness signals.

This module is pure: it imports no web framework and returns strings, so the
same snapshot can be asserted byte-for-byte in a unit test. Two functions:

* :func:`render` turns a snapshot into the exposition format -- a ``# HELP``
  line, a ``# TYPE`` line and then the samples for each metric, in the fixed
  order of :data:`METRICS`, with label values escaped and a trailing newline.
  A sample whose value is ``None`` (unknown, rather than zero) is omitted: an
  exposition must never invent a ``NaN`` for "we could not tell".
* :func:`collect` gathers the snapshot from the three producers the dashboard
  already reads -- :func:`pipeline.health_freshness.health_extras`,
  :func:`pipeline.outbox_health.outbox_summary` and
  :func:`pipeline.events_nats_consumer.consumer_status` -- plus the plan
  manifests' approval status. It NEVER raises: a producer that fails omits its
  metric and increments ``fagan_metrics_errors_total{source}``.

Secure by Design: every label value comes from a closed set (an approval
status, an error-source name) or from a broker durable name
(``pipeline-<event_type>``). No plan name, story key, file path, subject,
e-mail address or exception text is ever rendered -- telemetry is readable by
anything holding a scrape credential, so it carries aggregates only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline import parsers
from pipeline.events_nats_consumer import consumer_status
from pipeline.health_freshness import health_extras
from pipeline.outbox_health import outbox_summary

logger = logging.getLogger(__name__)

#: The plan approval statuses, in the fixed exposition order. ``none`` covers a
#: manifest with no approval record as well as an unrecognised one, so the
#: label value stays in this closed set whatever a manifest holds.
PLAN_STATUSES = ("approved", "pending_approval", "rejected", "none")

#: The producers whose failure is counted, as the ``source`` label values.
SOURCES = ("health_extras", "outbox_summary", "consumer_status", "plans")

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
        "fagan_metrics_errors_total",
        "counter",
        "Producer failures while collecting the metrics snapshot.",
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


def _manifest_names(plan_dir: str | Path) -> list[str]:
    """List the plan names under *plan_dir*, skipping unusable keys.

    This is the same listing the dashboard's ``GET /api/plans`` walks
    (``FileStore.list_manifests``), with the boundary check the plan routes
    apply (``pipeline.parsers._validate_key``) folded in here because the store
    helper is bound to the process-wide ``PLAN_DIR`` and cannot be pointed at
    an arbitrary directory. A name that would not survive that check is
    skipped rather than read: it could never address a manifest through the
    dashboard anyway.
    """
    names: list[str] = []
    for path in sorted(Path(plan_dir).glob("*.manifest.json")):
        name = path.name.removesuffix(".manifest.json")
        try:
            parsers._validate_key(name)
        except ValueError:
            continue
        names.append(name)
    return names


def _approval_status(manifest: Any) -> str:
    """Return *manifest*'s approval status, or ``"none"`` when it is not one."""
    if not isinstance(manifest, dict):
        return "none"
    approval = manifest.get("approval")
    status = approval.get("status") if isinstance(approval, dict) else None
    return status if status in PLAN_STATUSES else "none"


def _read_manifest(plan_dir: str | Path, plan_name: str) -> dict | None:
    """Read one manifest, or return ``None`` when it cannot be read."""
    try:
        payload = json.loads(
            (Path(plan_dir) / f"{plan_name}.manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _plan_samples(plan_dir: str | Path, counts: dict[str, int]) -> list[tuple[dict, int]]:
    """Count *plan_dir*'s manifests by approval status, in the fixed order."""
    for name in _manifest_names(plan_dir):
        manifest = _read_manifest(plan_dir, name)
        if manifest is None:
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


def collect(plan_dir: str | Path | None = None) -> dict[str, list[tuple[dict, Any]]]:
    """Return the metrics snapshot, never raising.

    *plan_dir* defaults to :data:`pipeline.paths.PLAN_DIR` and is resolved
    lazily, so this module stays importable without the full runtime
    configuration and a test can point the plan count at any directory.

    Each producer is called through this module's attribute
    (``metrics_exposition.health_extras``), which is the seam a test replaces;
    a producer that raises omits its metric and increments
    ``fagan_metrics_errors_total{source}``. The exception text is logged as a
    source name only -- a broker error can carry connection details.
    """
    if plan_dir is None:
        from pipeline import paths

        plan_dir = paths.PLAN_DIR
    errors = dict.fromkeys(SOURCES, 0)
    snapshot: dict[str, list[tuple[dict, Any]]] = {}

    try:
        _scheduler_samples(snapshot, health_extras(plan_dir))
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["health_extras"] += 1
        logger.warning("metrics producer failed", extra={"metrics_source": "health_extras"})

    try:
        _outbox_samples(snapshot, outbox_summary(plan_dir))
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
        snapshot["fagan_plans"] = _plan_samples(plan_dir, dict.fromkeys(PLAN_STATUSES, 0))
    except Exception:  # noqa: BLE001 - a probe failure must not break the scrape
        errors["plans"] += 1
        logger.warning("metrics producer failed", extra={"metrics_source": "plans"})

    snapshot["fagan_metrics_errors_total"] = [
        ({"source": source}, errors[source]) for source in SOURCES
    ]
    return snapshot
