"""Wiring assertions: PIPELINE_LOG_FORMAT must be honoured by both unattended entrypoints."""
import inspect

import app.pipeline_mcp_server as shim
import pipeline.scheduler_daemon as daemon
from pipeline import log_format
from pipeline.scheduler_daemon import run_daemon


def test_run_daemon_configures_logging_before_reading_interval_env():
    src = inspect.getsource(run_daemon)
    call_idx = src.index("configure_logging_from_env()")
    env_idx = src.index("PIPELINE_SCHEDULER_INTERVAL_S")
    assert call_idx < env_idx


def test_run_daemon_configures_logging_before_acquiring_the_lock():
    src = inspect.getsource(run_daemon)
    call_idx = src.index("configure_logging_from_env()")
    lock_idx = src.index("fcntl.flock(")
    assert call_idx < lock_idx


def test_run_daemon_imports_the_helper_locally():
    src = inspect.getsource(run_daemon)
    assert "from .log_format import configure_logging_from_env" in src


def test_shim_configures_logging_after_the_interpreter_guard():
    src = inspect.getsource(shim)
    main_idx = src.index('if __name__ == "__main__":')
    guard_idx = src.index("_assert_interpreter()", main_idx)
    call_idx = src.index("configure_logging_from_env()")
    assert main_idx < guard_idx < call_idx


def test_modules_import_cold_and_the_helper_is_reachable():
    assert hasattr(shim, "configure_logging_from_env")
    assert hasattr(log_format, "configure_logging_from_env")
    assert daemon.run_daemon is run_daemon
