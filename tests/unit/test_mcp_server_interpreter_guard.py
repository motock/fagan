"""Tests for the interpreter guard in app/pipeline_mcp_server.py."""

import importlib.util
from pathlib import Path

import pytest

SHIM_PATH = Path(__file__).resolve().parents[2] / "app" / "pipeline_mcp_server.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location(
        "pipeline_mcp_server_interpreter_guard_under_test", SHIM_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def shim():
    return _load_shim()


def test_assert_interpreter_raises_system_exit_when_problem(shim, monkeypatch):
    from pipeline import runtime_check

    monkeypatch.setattr(runtime_check, "interpreter_problem", lambda: "stale interpreter")
    run_calls = []
    monkeypatch.setattr(shim.mcp, "run", lambda *args, **kwargs: run_calls.append((args, kwargs)))

    with pytest.raises(SystemExit):
        shim._assert_interpreter()

    assert run_calls == []


def test_assert_interpreter_returns_none_when_no_problem(shim, monkeypatch):
    from pipeline import runtime_check

    monkeypatch.setattr(runtime_check, "interpreter_problem", lambda: None)

    assert shim._assert_interpreter() is None


def test_assert_interpreter_is_wired_before_mcp_run():
    text = SHIM_PATH.read_text()
    call_index = text.index("_assert_interpreter()\n")
    run_index = text.index("mcp.run()")
    assert call_index < run_index
