"""Harness-profile usage metrics: tokens, cost and call duration.

One usage record (``pipeline.usage_record.record_usage``) becomes three
OpenTelemetry instruments, and only when the deployment resolved to the
``harness`` telemetry profile:

* ``fagan.usage.tokens`` -- counter, unit ``{token}``, ``kind`` in
  {input, output, cache_creation, cache_read};
* ``fagan.usage.cost`` -- counter, unit ``USD``;
* ``fagan.usage.call.duration`` -- histogram, unit ``ms``.

Attributes are limited to ``kind``, ``role``, ``backend`` and ``model``: the
record's plan, story key, correlation id and attempt stay on the record
(cardinality), and content is never an attribute on either profile.

``role`` and ``backend`` must be 1-64 characters of ``[A-Za-z0-9._:-]``; the
whole record is skipped when either fails, *before* anything is emitted, so a
malformed value can never mint a series or leave a half-recorded sample. Zero is
a real measurement (a full cache hit, a free call) and is exported; only
``None`` means "not measured".

The ``customer`` profile registers nothing and exports nothing: model, token
counts and cost are harness-only metadata (docs/TELEMETRY_CONTRACT.md,
``## Profiles`` and ``## Data classification``). Nothing here reads the metrics
back -- the usage JSONL stays the billing-grade source of truth.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from . import telemetry_profile

logger = logging.getLogger(__name__)

TOKENS = "fagan.usage.tokens"
COST = "fagan.usage.cost"
DURATION = "fagan.usage.call.duration"

#: A bounded attribute value: the shape role and backend must have.
_LABEL_RE = re.compile(r"[A-Za-z0-9._:-]+")
_MAX_LABEL = 64

#: ``(kind attribute, record key)`` for the four token counts, in a fixed order.
_TOKEN_KINDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
    ("cache_read", "cache_read_input_tokens"),
)

_tokens: Any = None
_cost: Any = None
_duration: Any = None


def register(meter: Any) -> None:
    """Create the three instruments on *meter*, replacing any previous ones."""
    global _tokens, _cost, _duration
    _tokens = meter.create_counter(
        TOKENS, unit="{token}", description="Model tokens by kind."
    )
    _cost = meter.create_counter(
        COST, unit="USD", description="Estimated model call cost."
    )
    _duration = meter.create_histogram(
        DURATION, unit="ms", description="Model call duration."
    )


def reset() -> None:
    """Forget the instruments (``telemetry.reset_metrics``, tests)."""
    global _tokens, _cost, _duration
    _tokens = None
    _cost = None
    _duration = None


def _label(value: Any) -> str | None:
    """Return *value* as a bounded attribute value, or ``None`` when malformed.

    ``fullmatch`` rather than ``^...$``: ``$`` also matches before a trailing
    newline, so an anchored match would let ``"backend\\n"`` mint a series.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_LABEL:
        return None
    return value if _LABEL_RE.fullmatch(value) else None


def _number(value: Any) -> int | float | None:
    """Return *value* as a finite non-negative number, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def record(usage_record: dict) -> None:
    """Export one usage record's tokens, cost and duration. Never raises.

    Everything is validated before the first instrument is touched, so a
    rejected record exports nothing at all.
    """
    if not telemetry_profile.is_harness():
        return
    if _tokens is None or _cost is None or _duration is None:
        return
    if not isinstance(usage_record, dict):
        return

    role = _label(usage_record.get("role"))
    backend = _label(usage_record.get("backend"))
    if role is None or backend is None:
        logger.debug("usage metrics skipped: unusable role or backend")
        return

    counts = {}
    for kind, key in _TOKEN_KINDS:
        value = _number(usage_record.get(key))
        if value is None:
            logger.debug("usage metrics skipped: unusable token count")
            return
        counts[kind] = value

    # ``None`` means "not measured" and drops the sample; an unusable number is
    # dropped the same way rather than exported.
    cost = _number(usage_record.get("cost_usd"))
    duration = _number(usage_record.get("duration_ms"))

    # ``served_model`` is what actually served the call; a present-but-``None``
    # value must fall back to the requested model rather than label ``None``.
    model_value = usage_record.get("served_model")
    if model_value is None:
        model_value = usage_record.get("model")
    model = "" if model_value is None else str(model_value)

    labels = {"role": role, "backend": backend}
    for kind, value in counts.items():
        _tokens.add(value, {**labels, "kind": kind, "model": model})
    if cost is not None:
        _cost.add(cost, {**labels, "model": model})
    if duration is not None:
        _duration.record(duration, labels)
