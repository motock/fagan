"""Scoped usage records: which plan/story/attempt a billable call belongs to.

A provider call is billable to exactly one plan and story attempt, but the code
that receives the provider response (backend transports, log translators) does
not know which story is being worked on. Rather than thread ``plan_name`` /
``story_key`` through every provider call, the story-scoped entry point binds a
scope with :func:`usage_scope` (or the :func:`scoped_story_call` decorator) and
any code underneath records with :func:`record_usage` and no arguments about
attribution.

The scope lives in a :class:`contextvars.ContextVar`, the same pattern as
``pipeline.actor.bind_correlation_id``, so it is per-thread and per-task and
nesting restores the enclosing binding exactly.

A usage record is a per-plan JSONL sidecar with the FIXED key set in
:data:`USAGE_FIELDS`. The field set is closed on purpose: it is an accounting
artefact, so it carries counts, ids and cost - never prompts, completions or
any other payload. Values arrive from provider responses and are therefore
untrusted: token counts are sanitized to non-negative ints, money and duration
to non-negative numbers or ``None``, and a failure to write is dropped rather
than propagated, because losing an accounting line must never fail the work it
was accounting for.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from . import persistence
from .actor import current_correlation_id

#: The complete, closed key set of a usage record. Anything not listed here is
#: not an accounting fact and must not be written.
USAGE_FIELDS: tuple[str, ...] = (
    "ts", "plan", "story_key", "attempt", "correlation_id", "role", "backend",
    "model", "served_model", "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd", "duration_ms",
)

logger = logging.getLogger(__name__)

#: The scope bound by :func:`usage_scope`: ``{"plan", "story_key", "attempt"}``.
_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("pipeline_usage_scope", default=None)


def _token_count(value: Any) -> int:
    """Return *value* as a non-negative token count, or 0 when it is not one.

    ``bool`` is rejected explicitly: ``isinstance(True, int)`` is ``True``, and
    a flag is not a token count.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _measure(value: Any) -> int | float | None:
    """Return *value* as a non-negative number, or ``None`` when it is not one.

    A legitimate ``0`` (a cached call costing nothing, a sub-millisecond call)
    is preserved; only non-numbers and negatives become ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


@contextmanager
def usage_scope(
    plan_name: str, story_key: str, *, attempt: int | None = None
) -> Iterator[None]:
    """Bind the plan/story/attempt that records written inside this block belong to."""
    token = _SCOPE.set({"plan": plan_name, "story_key": story_key, "attempt": attempt})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope() -> dict[str, Any] | None:
    """Return a copy of the bound scope, or ``None`` when nothing is bound.

    A copy: the binding is shared by everything running under it, so a caller
    that mutates the returned dict must not be able to re-attribute later
    records.
    """
    bound = _SCOPE.get()
    return dict(bound) if bound is not None else None


def is_recording() -> bool:
    """True while a scope is bound, i.e. while ``record_usage`` would write."""
    return _SCOPE.get() is not None


def record_usage(
    *,
    role: str,
    backend: str,
    model: str,
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    cache_creation_input_tokens: Any = None,
    cache_read_input_tokens: Any = None,
    cost_usd: Any = None,
    duration_ms: Any = None,
    served_model: str | None = None,
    scope: dict[str, Any] | None = None,
) -> None:
    """Append one usage record for a provider call. Never raises.

    The attribution is *scope* when given (a drain thread recording a call it
    handled on another thread's behalf), else the scope bound on this thread.
    With neither, the call is not attributable and nothing is written - no
    record, no file.

    Every value comes from a provider response and is sanitized here; a write
    failure is logged as a warning naming only the exception type and dropped,
    so accounting can never break the work being accounted for.
    """
    try:
        bound = scope if scope is not None else current_scope()
        if bound is None:
            return

        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "plan": bound.get("plan"),
            "story_key": bound.get("story_key"),
            "attempt": bound.get("attempt"),
            "correlation_id": current_correlation_id() or None,
            "role": str(role),
            "backend": str(backend),
            "model": str(model),
            "served_model": None if served_model is None else str(served_model),
            "input_tokens": _token_count(input_tokens),
            "output_tokens": _token_count(output_tokens),
            "cache_creation_input_tokens": _token_count(cache_creation_input_tokens),
            "cache_read_input_tokens": _token_count(cache_read_input_tokens),
            "cost_usd": _measure(cost_usd),
            "duration_ms": _measure(duration_ms),
        }
        persistence._write_usage_record(record["plan"], record)
    except Exception as exc:  # noqa: BLE001 - accounting must never break a call
        logger.warning("usage record dropped", extra={"error_type": type(exc).__name__})


def scoped_story_call(fn):
    """Decorate a story-scoped method so its body runs inside a usage scope.

    The decorated method keeps its signature: ``(self, plan_name, story_key,
    ...)``. The two attribution arguments become the bound scope rather than
    being passed down to provider code that does not want them.
    """

    @functools.wraps(fn)
    def wrapper(self, plan_name, story_key, *args, **kwargs):
        with usage_scope(plan_name, story_key):
            return fn(self, plan_name, story_key, *args, **kwargs)

    return wrapper
