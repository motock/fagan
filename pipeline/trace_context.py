"""W3C trace-context helpers for attempt-scoped tracing.

An inbound ``traceparent`` header is caller-controlled, so it is accepted only
through :func:`parse_traceparent`, which requires exactly version ``00`` with a
32-character lowercase hex trace id, a 16-character lowercase hex span id and 2
hex flags, and rejects the all-zero ids W3C reserves as invalid. ``tracestate``
is never propagated: it is unbounded, caller-controlled data. Trace and span ids
never become metric labels, because that would make cardinality unbounded, and
nothing in this module logs a header value.

The ``opentelemetry`` import is deliberately lazy -- it happens inside
:func:`current_traceparent` -- so importing this module stays cheap and works
when the SDK is absent.
"""

from __future__ import annotations

import hashlib
import re
import secrets

TRACEPARENT_RE = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)

ATTEMPT_ROOT_SPAN_ID = "0000000000000001"

_ALL_ZERO_TRACE_ID = "0" * 32
_ALL_ZERO_SPAN_ID = "0" * 16


def parse_traceparent(value: object) -> dict[str, str] | None:
    """Return the ids from a valid W3C ``traceparent`` header, else ``None``.

    ``fullmatch`` (not ``match``) is required: with a ``$`` anchor ``match``
    also accepts a value with a caller-smuggled trailing newline. A non-string
    input returns ``None`` rather than raising, since the header comes from an
    untrusted caller.
    """
    if not isinstance(value, str):
        return None
    match = TRACEPARENT_RE.fullmatch(value)
    if match is None:
        return None
    trace_id = match["trace_id"]
    span_id = match["span_id"]
    if trace_id == _ALL_ZERO_TRACE_ID or span_id == _ALL_ZERO_SPAN_ID:
        return None
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "flags": match["flags"],
    }


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Build a ``traceparent`` value, raising ``ValueError`` on an invalid id.

    Validation runs through :func:`parse_traceparent` so the emitted header can
    always be read back by this module; the error message names the action only
    and never echoes the ids.
    """
    flags = "01" if sampled else "00"
    candidate = f"00-{trace_id}-{span_id}-{flags}"
    if parse_traceparent(candidate) is None:
        raise ValueError("format_traceparent: invalid trace id or span id")
    return candidate


def current_traceparent() -> str | None:
    """Return the ``traceparent`` of the active span, or ``None``.

    ``None`` covers every unavailable case: telemetry disabled, no active span,
    an invalid span context, or an absent SDK (which raises a bare
    ``ImportError`` when ``opentelemetry`` is masked in ``sys.modules``, so the
    broad ``except`` is load-bearing).
    """
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None
        trace_id = format(context.trace_id, "032x")
        span_id = format(context.span_id, "016x")
        flags = "01" if context.trace_flags.sampled else "00"
    except Exception:  # noqa: BLE001 - a tracing lookup must never break a caller
        return None
    return f"00-{trace_id}-{span_id}-{flags}"


def new_trace_id() -> str:
    """Return a fresh 32-character lowercase hex trace id, never all zero."""
    trace_id = secrets.token_hex(16)
    if trace_id == _ALL_ZERO_TRACE_ID:
        return "0" * 31 + "1"
    return trace_id


def attempt_trace_id(correlation_id: str, attempt: int) -> str:
    """Derive a deterministic trace id from a correlation id and attempt number.

    ``hashlib`` (not the salted builtin ``hash``) keeps the id stable across
    processes, and the literal NUL separator keeps ``("abc1", 23)`` distinct
    from ``("abc12", 3)``. Arguments are not validated; callers do that.
    """
    digest = hashlib.sha256(f"{correlation_id}\0{attempt}".encode()).hexdigest()[:32]
    if digest == _ALL_ZERO_TRACE_ID:
        return "0" * 31 + "1"
    return digest


_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def agent_traceparent(correlation_id: object, attempt: object) -> str | None:
    """Return the ``traceparent`` for one dispatch attempt, or ``None``.

    The trace id is :func:`attempt_trace_id` of ``(correlation_id, attempt)``,
    the same id the lifecycle sink files that attempt's ``agent_dispatched``
    span under, so the agent's own spans join the attempt that launched it.
    ``ATTEMPT_ROOT_SPAN_ID`` is the attempt's root span and the value is
    sampled.

    ``None`` means "stage nothing": the id is unusable when it is not a
    ``str`` of 1-64 ``[A-Za-z0-9._-]`` characters (a correlation id is
    caller-influenced, so it is validated rather than trusted), and the
    attempt is unusable when it is not a non-negative ``int`` -- ``bool`` is
    rejected even though it subclasses ``int``. Arguments are never logged.
    """
    if not isinstance(correlation_id, str) or (
        _CORRELATION_ID_RE.fullmatch(correlation_id) is None
    ):
        return None
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        return None
    return format_traceparent(
        attempt_trace_id(correlation_id, attempt),
        ATTEMPT_ROOT_SPAN_ID,
        sampled=True,
    )
