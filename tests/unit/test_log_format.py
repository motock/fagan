"""Tests for the opt-in JSON log format (``pipeline.log_format``).

The formatter exists so the structured fields call sites already pass via
``extra=`` actually reach a log file, and so the project's logging standard
(``timestamp``, ``level``, ``service``, ``correlation_id``, ``message``) is
true of this repo rather than aspirational. Two invariants are load-bearing
and pinned here: the handler stays on stderr (stdout belongs to the MCP stdio
transport) and the root logger's level is never touched, so opting in changes
how lines are rendered, not which lines are emitted.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

import pytest

from pipeline.actor import bind_correlation_id
from pipeline.log_format import JsonLogFormatter, configure_logging_from_env


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    """Never leave a JSON handler installed for the rest of the worker."""
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        yield
    finally:
        root.handlers = saved


def test_unset_env_returns_false() -> None:
    """No opt-in means the default text configuration is left alone."""
    assert configure_logging_from_env({}) is False


def test_unset_env_leaves_handlers_untouched() -> None:
    """The default path must not replace whatever configured logging first."""
    root = logging.getLogger()
    root.handlers = [logging.StreamHandler()]
    saved_list = list(root.handlers)
    configure_logging_from_env({})
    assert root.handlers == saved_list


def test_json_env_with_whitespace_and_case_returns_true() -> None:
    """The switch ignores case and surrounding whitespace."""
    assert configure_logging_from_env({"PIPELINE_LOG_FORMAT": " JSON "}) is True


def test_json_env_installs_exactly_one_handler() -> None:
    """One handler, so a line is never printed twice in two formats."""
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"})
    assert len(logging.getLogger().handlers) == 1


def test_json_env_handler_formatter_is_json_formatter() -> None:
    """The installed handler renders records as JSON."""
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"})
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonLogFormatter)


def test_text_value_returns_false() -> None:
    """Only the exact value ``json`` opts in."""
    assert configure_logging_from_env({"PIPELINE_LOG_FORMAT": "text"}) is False


def test_zero_value_returns_false() -> None:
    """``0`` is not a truthy-style opt-in."""
    assert configure_logging_from_env({"PIPELINE_LOG_FORMAT": "0"}) is False


def test_true_value_returns_false() -> None:
    """``true`` is not the documented switch value."""
    assert configure_logging_from_env({"PIPELINE_LOG_FORMAT": "true"}) is False


def test_text_value_leaves_handlers_untouched() -> None:
    """A non-json value changes nothing about the root logger."""
    root = logging.getLogger()
    root.handlers = [logging.StreamHandler()]
    saved_list = list(root.handlers)
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "text"})
    assert root.handlers == saved_list


def test_second_call_returns_true() -> None:
    """Configuration is idempotent: a second call still reports installed."""
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"})
    assert configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"}) is True


def test_second_call_does_not_add_handler() -> None:
    """A second call must not stack a duplicate handler."""
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"})
    configure_logging_from_env({"PIPELINE_LOG_FORMAT": "json"})
    assert len(logging.getLogger().handlers) == 1


def test_plain_record_has_exactly_standard_keys() -> None:
    """A record with no extra= carries exactly the five standard fields."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert set(payload) == {
        "timestamp",
        "level",
        "service",
        "correlation_id",
        "message",
    }


def test_service_is_logger_name() -> None:
    """``service`` names the emitting logger."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["service"] == "svc"


def test_level_is_level_name() -> None:
    """``level`` is the level name, not the numeric level."""
    record = logging.LogRecord("svc", logging.WARNING, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["level"] == "WARNING"


def test_message_interpolates_args() -> None:
    """%-style args are interpolated into the message field."""
    record = logging.LogRecord(
        "svc", logging.INFO, __file__, 1, "story %s failed", ("S1",), None
    )
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["message"] == "story S1 failed"


def test_timestamp_parses_with_fromisoformat() -> None:
    """``timestamp`` is a real ISO-8601 instant, not an opaque string."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert datetime.fromisoformat(payload["timestamp"]) is not None


def test_timestamp_ends_with_utc_offset() -> None:
    """Timestamps are UTC and say so."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["timestamp"].endswith("+00:00")


def test_extra_values_become_top_level_fields() -> None:
    """Fields a call site passed via extra= reach the log line."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    record.plan = "p"
    record.story_key = "S1"
    payload = json.loads(JsonLogFormatter().format(record))
    assert (payload["plan"], payload["story_key"]) == ("p", "S1")


def test_correlation_id_from_context() -> None:
    """The correlation id is read from the request context, not the record."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    with bind_correlation_id("req-1"):
        payload = json.loads(JsonLogFormatter().format(record))
    assert payload["correlation_id"] == "req-1"


def test_correlation_id_empty_outside_request() -> None:
    """Outside a request the field is an empty string, never null."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["correlation_id"] == ""


def test_non_serializable_extra_renders_as_string() -> None:
    """An extra= value that is not JSON-safe degrades to its repr."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "msg", (), None)
    record.payload = object()
    assert isinstance(json.loads(JsonLogFormatter().format(record)), dict)


def test_exc_info_renders_exception_field() -> None:
    """A record carrying exc_info renders the traceback in ``exception``."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "svc", logging.ERROR, __file__, 1, "msg", (), sys.exc_info()
        )
    payload = json.loads(JsonLogFormatter().format(record))
    assert "boom" in payload["exception"]
