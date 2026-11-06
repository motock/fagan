"""Tests for the W3C trace-context helpers in ``pipeline.trace_context``."""
import pipeline.server  # noqa: F401, I001
import re
import sys

import pytest

# The inbound `traceparent` header is caller-controlled, so these tests pin the
# acceptance boundary: only exactly version `00` with a 32-character lowercase
# hex trace id, a 16-character lowercase hex span id and 2 hex flags is
# accepted, and neither id may be all zeros. Anything else -- including a
# trailing newline (which `re.match` with a `$` anchor would accept) and a
# `tracestate` suffix -- parses to None.
from pipeline.trace_context import (
    ATTEMPT_ROOT_SPAN_ID,
    TRACEPARENT_RE,
    attempt_trace_id,
    current_traceparent,
    format_traceparent,
    new_trace_id,
    parse_traceparent,
)

W3C_EXAMPLE = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def test_parse_accepts_w3c_spec_example():
    assert parse_traceparent(W3C_EXAMPLE) == {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "flags": "01",
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        12345,
        "",
        "00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01",
        "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "00-" + "0" * 32 + "-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-" + "0" * 16 + "-01",
        "00-0af7651916cd43dd8448eb211c80319-b7ad6b7169203331-01",
        W3C_EXAMPLE + "\n",
        W3C_EXAMPLE + "-congo=t61rcWkgMzE",
    ],
)
def test_parse_rejects_malformed_values(value):
    assert parse_traceparent(value) is None


def test_format_round_trips_through_parse():
    traceparent = format_traceparent(TRACE_ID, SPAN_ID)
    assert parse_traceparent(traceparent) == {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "flags": "01",
    }


def test_format_unsampled_yields_zero_flags():
    traceparent = format_traceparent(TRACE_ID, SPAN_ID, sampled=False)
    assert traceparent.endswith("-00")
    assert parse_traceparent(traceparent)["flags"] == "00"


@pytest.mark.parametrize(
    "trace_id, span_id",
    [
        ("0" * 32, SPAN_ID),
        ("not-hex", SPAN_ID),
    ],
)
def test_format_rejects_bad_ids(trace_id, span_id):
    with pytest.raises(ValueError):
        format_traceparent(trace_id, span_id)


def test_current_traceparent_is_none_without_active_span():
    assert current_traceparent() is None


def test_current_traceparent_is_none_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    assert current_traceparent() is None


def test_new_trace_id_is_32_lowercase_hex_and_differs():
    first = new_trace_id()
    second = new_trace_id()
    assert HEX32.fullmatch(first) is not None
    assert HEX32.fullmatch(second) is not None
    assert first != second


def test_attempt_trace_id_is_deterministic_and_distinguishes_inputs():
    first = attempt_trace_id("abc123", 0)
    assert HEX32.fullmatch(first) is not None
    assert attempt_trace_id("abc123", 0) == first
    assert attempt_trace_id("abc123", 1) != first
    assert attempt_trace_id("abc124", 0) != first


def test_attempt_root_span_id_is_the_lowest_non_zero_span_id():
    assert ATTEMPT_ROOT_SPAN_ID == "0000000000000001"


def test_traceparent_re_fullmatches_w3c_example():
    assert TRACEPARENT_RE.fullmatch(W3C_EXAMPLE) is not None
