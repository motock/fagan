"""Wiring assertions for the dashboard's opt-in JSON log format (P3-04).

These tests pin *wiring only*: that ``_log_startup_preflight`` opts in before
its own ``try:`` (so a failing preflight cannot swallow the opt-in), that the
default leaves the root logger untouched, and that ``PIPELINE_LOG_FORMAT=json``
installs a :class:`JsonLogFormatter` handler.

The handler list is process-global and shared by every later test in the same
xdist worker, so the JSON case restores it in a ``finally`` and then asserts the
restore actually happened.
"""

from __future__ import annotations

import inspect
import logging

from app.dashboard import _log_startup_preflight
from pipeline.log_format import JsonLogFormatter


def test_preflight_calls_configure_logging_before_try() -> None:
    """The opt-in must not depend on the preflight run succeeding."""
    src = inspect.getsource(_log_startup_preflight)
    assert "configure_logging_from_env()" in src
    assert src.index("configure_logging_from_env()") < src.index("try:")


def test_default_off_leaves_root_handlers_untouched(monkeypatch) -> None:
    """With the env var unset the dashboard path must not touch root handlers."""
    monkeypatch.delenv("PIPELINE_LOG_FORMAT", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)

    _log_startup_preflight()

    assert root.handlers == before


def test_json_env_installs_handler_and_restores(monkeypatch) -> None:
    """``PIPELINE_LOG_FORMAT=json`` installs a JSON handler; the test restores it."""
    root = logging.getLogger()
    original = list(root.handlers)
    original_ids = [id(handler) for handler in original]
    monkeypatch.setenv("PIPELINE_LOG_FORMAT", "json")
    try:
        _log_startup_preflight()
        assert any(
            isinstance(getattr(handler, "formatter", None), JsonLogFormatter)
            for handler in root.handlers
        )
    finally:
        root.handlers[:] = original

    assert root.handlers == original
    assert [id(handler) for handler in root.handlers] == original_ids
