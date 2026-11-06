"""Trace ids on JSON log lines, added only when a traceparent is active.

The formatter enriches each line with ``trace_id``/``span_id`` so a log line can
be correlated with the trace that produced it. Two invariants are pinned here:

* untraced output is unchanged -- byte for byte the same key set as before the
  enrichment existed, with no empty placeholders; and
* the ids come only from ``pipeline.trace_context.parse_traceparent``, so a
  caller-controlled header that is malformed (wrong case, all-zero ids, plain
  garbage) is dropped rather than logged, and the raw header never reaches the
  line.

The lookups are exercised on a *second* call on the same formatter instance
too, because caching the ids or latching a "tracing is broken" flag after one
failure silently corrupts every later line.

``pipeline.server`` is imported explicitly first; import order is not required
for cold-importability (every pipeline module imports cold, see
tests/unit/test_hub_satellite_cold_imports.py).
"""

from __future__ import annotations

import json
import logging

import pytest

import pipeline.server  # noqa: F401 - imported first to break a pre-existing cycle
from pipeline import log_format as log_format_module
from pipeline.log_format import JsonLogFormatter

#: The example header from the W3C trace-context specification.
SPEC_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

SPEC_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPEC_SPAN_ID = "b7ad6b7169203331"

#: The key set an untraced line must keep exactly.
BASE_KEYS = frozenset({"timestamp", "level", "service", "correlation_id", "message"})

TRACED_KEYS = BASE_KEYS | {"trace_id", "span_id"}


def _record() -> logging.LogRecord:
    """Return a plain INFO record with no ``extra=`` fields."""
    return logging.LogRecord(
        name="pipeline.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unit test line",
        args=(),
        exc_info=None,
    )


def _payload(formatter: JsonLogFormatter) -> dict[str, object]:
    """Format one record and decode the resulting JSON line."""
    payload = json.loads(formatter.format(_record()))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def formatter() -> JsonLogFormatter:
    """A formatter instance reused across calls within one test."""
    return JsonLogFormatter()


def test_active_span_adds_trace_and_span_ids(
    monkeypatch: pytest.MonkeyPatch, formatter: JsonLogFormatter
) -> None:
    """A parseable active traceparent puts both ids on the line."""
    monkeypatch.setattr(
        "pipeline.trace_context.current_traceparent", lambda: SPEC_TRACEPARENT
    )

    payload = _payload(formatter)

    assert payload["trace_id"] == SPEC_TRACE_ID
    assert payload["span_id"] == SPEC_SPAN_ID
    assert set(payload) == set(TRACED_KEYS)
    # The lookup is imported at call time inside ``format``: the formatter
    # module never imports the SDK (nor the helper) at import time.
    assert not hasattr(log_format_module, "current_traceparent")

    # Second call on the same instance: no active span, and the ids from the
    # first call must not leak into it.
    monkeypatch.setattr("pipeline.trace_context.current_traceparent", lambda: None)

    untraced = _payload(formatter)

    assert set(untraced) == set(BASE_KEYS)
    assert "trace_id" not in untraced


def test_no_active_span_keeps_the_pre_change_key_set(
    monkeypatch: pytest.MonkeyPatch, formatter: JsonLogFormatter
) -> None:
    """No traceparent means no trace keys at all -- not empty placeholders."""
    monkeypatch.setattr("pipeline.trace_context.current_traceparent", lambda: None)

    payload = _payload(formatter)

    assert set(payload) == set(BASE_KEYS)
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_lookup_failure_never_breaks_log_emission(
    monkeypatch: pytest.MonkeyPatch, formatter: JsonLogFormatter
) -> None:
    """A raising lookup drops the ids, logs no exception text, and recovers."""

    def _boom() -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr("pipeline.trace_context.current_traceparent", _boom)

    line = formatter.format(_record())
    payload = json.loads(line)

    assert set(payload) == set(BASE_KEYS)
    assert "trace_id" not in payload
    assert "span_id" not in payload
    assert "boom" not in line

    # A later line with tracing working again must carry the ids: one failure
    # must not latch tracing off for the life of the formatter.
    monkeypatch.setattr(
        "pipeline.trace_context.current_traceparent", lambda: SPEC_TRACEPARENT
    )

    recovered = _payload(formatter)

    assert set(recovered) == set(TRACED_KEYS)
    assert recovered["trace_id"] == SPEC_TRACE_ID


def test_malformed_traceparents_are_dropped(
    monkeypatch: pytest.MonkeyPatch, formatter: JsonLogFormatter
) -> None:
    """Anything ``parse_traceparent`` rejects yields no trace keys, no raw value."""
    uppercase = "00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01"
    zero_trace_id = "00-00000000000000000000000000000000-b7ad6b7169203331-01"

    monkeypatch.setattr(
        "pipeline.trace_context.current_traceparent", lambda: uppercase
    )
    upper_line = formatter.format(_record())
    upper_payload = json.loads(upper_line)
    assert set(upper_payload) == set(BASE_KEYS)
    assert "trace_id" not in upper_payload
    assert "span_id" not in upper_payload
    assert "0AF7651916CD43DD8448EB211C80319C" not in upper_line

    monkeypatch.setattr(
        "pipeline.trace_context.current_traceparent", lambda: zero_trace_id
    )
    assert set(_payload(formatter)) == set(BASE_KEYS)

    monkeypatch.setattr("pipeline.trace_context.current_traceparent", lambda: "garbage")
    garbage_line = formatter.format(_record())
    assert set(json.loads(garbage_line)) == set(BASE_KEYS)
    assert "garbage" not in garbage_line

    monkeypatch.setattr(
        "pipeline.trace_context.current_traceparent", lambda: SPEC_TRACEPARENT
    )
    assert set(_payload(formatter)) == set(TRACED_KEYS)
