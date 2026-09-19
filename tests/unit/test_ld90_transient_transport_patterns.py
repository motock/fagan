"""LD90-W5-01: reviewer transport failures (5xx / timeouts) are transient.

Reviewer transport failures (HTTP 502/503/504, gateway/read timeouts) must be
classified as transient backend errors rather than charged to the story as an
inconclusive review. Two mechanisms are graded here:

* ``_is_transient_backend_error`` - the text check, extended with 5xx /
  bad-gateway / service-unavailable / timed-out signatures.
* ``_is_transient_backend_exception`` - a new classifier that walks the
  ``__cause__``/``__context__`` chain, because backends wrap transport errors
  in a ``RuntimeError`` chained ``from`` the original httpx exception.

Only what this story adds is asserted: the new pattern behaviour, the new
helper, its ``__all__`` entry, and the fixed prefix of the pre-existing
patterns. The shared ``__all__`` list and the pattern list are extended by
later stories, so membership/prefix is graded rather than exact contents.
"""

from __future__ import annotations

import inspect

import pytest

from pipeline import parsers
from tests.unit._pipeline_mcp_server_test_helpers import _RATE_LIMIT_MSG

# The four pre-existing transient patterns must stay first and in order; later
# stories append to this list, so only this fixed prefix is asserted.
_PREEXISTING_TRANSIENT_PATTERNS = [
    r"500\s+internal\s+server\s+error",
    r"internal\s+server\s+error",
    r"connection\s+reset",
    r"connection\s+refused",
]


def _chained(cause: BaseException, message: str) -> BaseException:
    """Build ``RuntimeError(message)`` raised ``from`` ``cause``, as the Ollama
    backend does when it wraps a transport error."""
    try:
        raise RuntimeError(message) from cause
    except RuntimeError as exc:  # pragma: no cover - always taken
        return exc


def _wrap(exc: BaseException, depth: int) -> BaseException:
    """Chain ``depth`` RuntimeErrors around ``exc`` via ``raise ... from``."""
    for i in range(depth):
        exc = _chained(exc, f"backend errored after 3 attempt(s): wrap {i}")
    return exc


# --------------------------------------------------------------------------
# Text patterns
# --------------------------------------------------------------------------


def test_preexisting_transient_patterns_kept_first_and_in_order():
    assert (
        parsers._TRANSIENT_BACKEND_PATTERNS[: len(_PREEXISTING_TRANSIENT_PATTERNS)]
        == _PREEXISTING_TRANSIENT_PATTERNS
    )


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 502 Bad Gateway",
        "bad gateway from upstream",
        "503 Service Unavailable",
        "504 Gateway Time-out",
        "gateway timeout",
        "httpx.ReadTimeout: Read timed out",
        "LLM call failed: timed out",
    ],
)
def test_transient_text_patterns_positive(text):
    assert parsers._is_transient_backend_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "VERDICT: APPROVE\nLooks good.",
        "the retry loop handles timeouts via a deadline",
        _RATE_LIMIT_MSG,
    ],
)
def test_transient_text_patterns_negative(text):
    assert parsers._is_transient_backend_error(text) is False


def test_rate_limit_message_is_not_transient_but_is_rate_limited():
    # The rate-limit banner stays in its own bucket: it must not be reclassified
    # as a transient transport failure.
    assert parsers._is_rate_limited(_RATE_LIMIT_MSG) is True
    assert parsers._is_transient_backend_error(_RATE_LIMIT_MSG) is False


def test_transient_docstring_mentions_5xx_and_timeouts_and_keeps_unknown_note():
    doc = parsers._is_transient_backend_error.__doc__ or ""
    # Collapse whitespace and hyphens so wrapped lines and "bad-gateway" /
    # "timed-out" spellings still match.
    normalized = " ".join(doc.lower().replace("-", " ").split())
    first_line = " ".join(doc.splitlines()[0].lower().replace("-", " ").split())
    assert "5xx" in first_line
    assert "bad gateway" in normalized
    assert "service unavailable" in normalized
    assert "timed out" in normalized
    # The paragraph about being called only after _parse_verdict returns UNKNOWN
    # is retained.
    assert "_parse_verdict returns UNKNOWN" in doc


def test_new_transient_patterns_are_registered():
    # Membership only: later stories append further patterns to this list.
    for pattern in (
        r"502\s+bad\s+gateway",
        r"bad\s+gateway",
        r"503\s+service\s+unavailable",
        r"service\s+unavailable",
        r"gateway\s+time-?out",
        r"read\s+timed\s+out",
        r"\btimed\s+out\b",
    ):
        assert pattern in parsers._TRANSIENT_BACKEND_PATTERNS


def test_rate_limit_survivors_are_intact():
    # _RATE_LIMIT_PATTERNS / _is_rate_limited are survivors of this story and
    # must keep working unchanged.
    for pattern in (
        r"hit your session limit",
        r"usage limit reached",
        r"out_of_credits",
        r"overageDisabledReason",
    ):
        assert pattern in parsers._RATE_LIMIT_PATTERNS
    assert parsers._is_rate_limited("usage limit reached") is True
    assert parsers._is_rate_limited("VERDICT: APPROVE\nLooks good.") is False


# --------------------------------------------------------------------------
# Exception classifier
# --------------------------------------------------------------------------


def test_exception_classifier_is_exported_and_callable():
    assert callable(parsers._is_transient_backend_exception)
    assert "_is_transient_backend_exception" in parsers.__all__
    # Inserted after the text-check name (ordering relative to a fixed anchor;
    # later stories append their own names elsewhere in the list).
    assert parsers.__all__.index("_is_transient_backend_exception") > parsers.__all__.index(
        "_is_transient_backend_error"
    )


def test_exception_classifier_defined_immediately_after_text_check():
    module_lines = inspect.getsourcelines(parsers)[0]
    text_check_line = next(
        i
        for i, line in enumerate(module_lines)
        if line.startswith("def _is_transient_backend_error(")
    )
    exc_check_line = next(
        i
        for i, line in enumerate(module_lines)
        if line.startswith("def _is_transient_backend_exception(")
    )
    assert exc_check_line > text_check_line
    # No other top-level def is defined between the two helpers.
    between = module_lines[text_check_line + 1 : exc_check_line]
    assert not any(line.startswith("def ") for line in between)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionRefusedError(),
    ],
    ids=["TimeoutError", "ConnectionRefusedError"],
)
def test_builtin_transport_exceptions_are_transient(exc):
    assert parsers._is_transient_backend_exception(exc) is True


def test_httpx_style_timeout_type_name_is_transient():
    assert parsers._is_transient_backend_exception(ReadTimeout("x")) is True


def test_wrapped_transport_error_is_transient_via_cause_chain():
    exc = _chained(ReadTimeout("x"), "backend errored after 3 attempt(s): boom")
    assert exc.__cause__ is not None
    assert parsers._is_transient_backend_exception(exc) is True


def test_wrapped_transport_error_is_transient_via_context_chain():
    try:
        try:
            raise TimeoutError("read timed out")
        except TimeoutError:
            raise RuntimeError("backend errored after 3 attempt(s): boom")
    except RuntimeError as exc:
        assert exc.__cause__ is None
        assert parsers._is_transient_backend_exception(exc) is True


def test_runtime_error_with_5xx_message_and_no_cause_is_transient():
    exc = RuntimeError("errored after 3 attempt(s) in 180.0s: 502 Bad Gateway")
    assert exc.__cause__ is None
    assert parsers._is_transient_backend_exception(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("malformed tool call shape"),
        RuntimeError("boom"),
    ],
    ids=["ValueError", "RuntimeError"],
)
def test_non_transport_exceptions_are_not_transient(exc):
    assert parsers._is_transient_backend_exception(exc) is False


def test_exception_cycle_terminates_and_is_not_transient():
    exc = RuntimeError("boom")
    exc.__cause__ = exc
    assert parsers._is_transient_backend_exception(exc) is False


def test_transport_error_within_five_link_chain_is_transient():
    # The transport error sits 4 links below the outer exception (5th link).
    exc = _wrap(TimeoutError("read timed out"), 4)
    assert parsers._is_transient_backend_exception(exc) is True


def test_transport_error_beyond_five_link_chain_is_not_transient():
    # The transport error sits 5 links below the outer exception (6th link),
    # beyond the documented 5-link walk.
    exc = _wrap(TimeoutError("read timed out"), 5)
    assert parsers._is_transient_backend_exception(exc) is False


class ReadTimeout(Exception):
    """Ad-hoc stand-in for httpx.ReadTimeout (name contains "Timeout")."""