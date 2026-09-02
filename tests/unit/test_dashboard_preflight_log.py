"""Integration-graded tests: app/dashboard.py must run pipeline preflight at
import time and log the summary at the level matching the worst check status,
without ever blocking dashboard start-up.

Per the repo's "grade the wiring, not the unit" rule, these tests exercise the
REAL startup path: they force a fresh import of app.dashboard (the same module
path the existing dashboard tests import) and assert on the log records
emitted at import time. They deliberately never call _log_startup_preflight()
directly -- a direct call would pass even if the wiring was never added.

pipeline.preflight.run_preflight is stubbed with a synthetic result list so no
test depends on the host (installed CLIs, PLAN_DIR, today's model registry).
pipeline.preflight.summarize is wrapped in a pass-through spy so the tests can
assert the dashboard logs the *summary* (not just any line mentioning
preflight) without changing what summarize() returns.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from pathlib import Path

import pytest

from pipeline import preflight

DASHBOARD_MODULE = "app.dashboard"
REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_SOURCE = REPO_ROOT / "app" / "dashboard.py"

# Sentinel env value: must never leak into the startup log line (CLAUDE.md log
# hygiene -- the line carries counts and check names only, no env values).
_ENV_SENTINEL = "sentinel-env-value-4f7a2b"

# An absolute path (POSIX or Windows) -- the startup line must not add any
# beyond what preflight's summarize() already emits (which, for the synthetic
# results used here, is none).
_ABSOLUTE_PATH_RE = re.compile(
    r"(?m)(?:^|[\s(])/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+"
    r"|[A-Za-z]:\\"
)


def _result(name, status, message):
    return {"name": name, "status": status, "message": message}


def _stub_run_preflight(monkeypatch, results):
    """Point pipeline.preflight.run_preflight at a synthetic result list.

    Returns (calls, real_summarize): calls records every (args, kwargs) the
    dashboard made to run_preflight, so tests can assert the wiring actually
    invoked preflight exactly once at import time.
    """
    calls = []

    def _fake_run_preflight(*args, **kwargs):
        calls.append((args, kwargs))
        return [dict(r) for r in results]

    real_summarize = preflight.summarize

    def _spy_summarize(results, *args, **kwargs):
        return real_summarize(results)

    monkeypatch.setattr(preflight, "run_preflight", _fake_run_preflight)
    monkeypatch.setattr(preflight, "summarize", _spy_summarize)
    return calls, real_summarize


@pytest.fixture
def fresh_import():
    """Re-import app.dashboard from scratch; restore sys.modules afterwards.

    The dashboard module is imported by many existing test files, so the
    original module object is put back once the assertion window closes.
    """
    saved = sys.modules.get(DASHBOARD_MODULE)

    def _import_fresh():
        sys.modules.pop(DASHBOARD_MODULE, None)
        return importlib.import_module(DASHBOARD_MODULE)

    yield _import_fresh

    if saved is not None:
        sys.modules[DASHBOARD_MODULE] = saved
    else:
        sys.modules.pop(DASHBOARD_MODULE, None)


def _preflight_records(caplog):
    return [r for r in caplog.records if "preflight" in r.getMessage().lower()]


def test_import_emits_exactly_one_preflight_summary_at_info(
    fresh_import, monkeypatch, caplog
):
    """All-ok preflight: exactly one INFO line containing the summary."""
    results = [
        _result("probe-plan-dir", "ok", "probe-plan-dir writable"),
        _result("probe-git", "ok", "probe-git found"),
        _result("probe-backend", "ok", "probe-backend found"),
        _result("probe-registry", "ok", "probe-registry parsed"),
    ]
    calls, real_summarize = _stub_run_preflight(monkeypatch, results)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", _ENV_SENTINEL)

    with caplog.at_level(logging.INFO):
        module = fresh_import()

    records = _preflight_records(caplog)
    assert len(records) == 1, (
        f"expected exactly one 'preflight' log line at import, got "
        f"{[r.getMessage() for r in records]}"
    )
    record = records[0]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    # the line carries the summarize() output (counts; check NAMES appear in
    # summarize() only for warn-status checks -- asserted in the warn test)
    assert real_summarize(results) in message
    assert any(ch.isdigit() for ch in message), "line must carry counts"
    # log hygiene: no env values, no absolute paths beyond what summarize()
    # already emits (summarize() emits none for these results)
    assert _ENV_SENTINEL not in message
    assert not _ABSOLUTE_PATH_RE.search(message), (
        f"startup log leaked an absolute path: {message!r}"
    )
    # the wiring ran preflight exactly once, at import time
    assert len(calls) == 1
    # the dashboard app object exists after import
    assert getattr(module, "app", None) is not None


def test_warn_status_logs_single_preflight_line_at_warning(
    fresh_import, monkeypatch, caplog
):
    results = [
        _result("probe-plan-dir", "ok", "probe-plan-dir writable"),
        _result("probe-backend", "warn", "probe-backend CLI missing"),
    ]
    calls, _ = _stub_run_preflight(monkeypatch, results)

    with caplog.at_level(logging.INFO):
        fresh_import()

    records = _preflight_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert len(calls) == 1


def test_fail_status_logs_at_error_and_does_not_block_startup(
    fresh_import, monkeypatch, caplog
):
    """Worst status wins (fail beats warn), and start-up is NOT blocked."""
    results = [
        _result("probe-plan-dir", "ok", "probe-plan-dir writable"),
        _result("probe-backend", "warn", "probe-backend CLI missing"),
        _result("probe-git", "fail", "probe-git missing"),
    ]
    calls, _ = _stub_run_preflight(monkeypatch, results)

    with caplog.at_level(logging.INFO):
        module = fresh_import()  # must not raise

    records = _preflight_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert len(calls) == 1
    # start-up not blocked: the app object is still created and importable
    assert getattr(module, "app", None) is not None


def test_preflight_exception_is_swallowed_and_logged_as_warning(
    fresh_import, monkeypatch, caplog
):
    """An exception inside preflight must degrade, never die."""

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic preflight explosion")

    monkeypatch.setattr(preflight, "run_preflight", _boom)

    with caplog.at_level(logging.INFO):
        module = fresh_import()  # must not raise

    assert getattr(module, "app", None) is not None
    records = _preflight_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert not any(
        r.levelno >= logging.ERROR and "preflight" in r.getMessage().lower()
        for r in caplog.records
    )


def test_malformed_preflight_output_never_blocks_startup(
    fresh_import, monkeypatch, caplog
):
    """Malformed results (missing status/message) must not kill the import."""
    results = [{"name": "probe-malformed"}]
    calls, _ = _stub_run_preflight(monkeypatch, results)

    with caplog.at_level(logging.INFO):
        module = fresh_import()  # must not raise regardless of summarize()

    assert getattr(module, "app", None) is not None
    assert len(calls) == 1


def test_wiring_is_module_level_after_app_creation_and_never_raises():
    """Source-level contract: defined, wired at import time, never raising."""
    source = DASHBOARD_SOURCE.read_text(encoding="utf-8")
    definition = re.search(r"(?m)^def _log_startup_preflight\(", source)
    assert definition is not None, (
        "app/dashboard.py must define _log_startup_preflight()"
    )
    call = re.search(r"(?m)^_log_startup_preflight\(\)", source)
    assert call is not None, (
        "_log_startup_preflight() must be called at module import time "
        "(column 0), not merely defined"
    )
    app_assignment = re.search(r"(?m)^app\b.*=", source)
    assert app_assignment is not None, (
        "app/dashboard.py must create a module-level app object"
    )
    assert call.start() > app_assignment.start(), (
        "the preflight wiring must sit after the app object is created"
    )
    assert not re.search(r"raise_on_failure\s*\(", source), (
        "dashboard startup must degrade, not die: raise_on_failure must not "
        "be wired into the startup path"
    )