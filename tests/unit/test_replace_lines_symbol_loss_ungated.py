"""Regression: replace_lines must reject top-level symbol loss even when the
deletion classifier sees nothing to report (the rename bypass).

Mode 55 defect: ``_dropped_top_level_defs`` was only consulted inside the
``if deletions and not confirm_removals`` gate. Renaming ``def foo()`` to
``def foo_v2()`` classifies the old def lines as rewrites (similarity ratio
>= 0.9), so ``deletions`` was empty, the gate was skipped entirely, the write
landed, and the original symbols were silently gone.

Both agent variants are exercised because they are near-verbatim copies and a
fix to only one leaves the oracle path broken.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / "scripts" / f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["local_agent", "local_agent_oracle"])
def agent(request, tmp_path, monkeypatch):
    # The env-isolation conftest fixture clears LOCAL_AGENT_MODEL per test, and
    # both scripts read it at import time, so set it before loading.
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    mod = _load(request.param)
    monkeypatch.setattr(mod, "CWD", tmp_path)
    return mod


TWO_DEFS = (
    "def read_plist_env():\n"
    "    return {}\n"
    "\n"
    "\n"
    "def read_mcp_server_env():\n"
    "    return {}\n"
)

RENAMED_DEFS = (
    "def read_plist_env_v2():\n"
    "    return {}\n"
    "\n"
    "\n"
    "def read_mcp_server_env_v2():\n"
    "    return {}\n"
)


def test_rename_of_top_level_defs_is_rejected(agent, tmp_path):
    # arrange: two top-level defs, nothing else
    target = tmp_path / "env.py"
    target.write_text(TWO_DEFS)

    # act: replace the whole range with renamed versions of the same defs
    result = agent.run_tool("replace_lines", {
        "path": "env.py", "start": 1, "end": 6, "new_str": RENAMED_DEFS,
    })

    # assert: the rename must not silently drop the original symbols
    assert "The edit was NOT applied" in result
    assert result.startswith("ERROR")
    assert "read_plist_env" in result
    assert "read_mcp_server_env" in result
    assert target.read_text() == TWO_DEFS


def test_rename_with_confirm_removals_succeeds(agent, tmp_path):
    # arrange
    target = tmp_path / "env.py"
    target.write_text(TWO_DEFS)

    # act: same edit, explicitly confirmed
    result = agent.run_tool("replace_lines", {
        "path": "env.py", "start": 1, "end": 6, "new_str": RENAMED_DEFS,
        "confirm_removals": True,
    })

    # assert: confirmation still lets the edit through
    assert "The edit was NOT applied" not in result
    written = target.read_text()
    assert "def read_plist_env_v2():" in written
    assert "def read_mcp_server_env_v2():" in written


def test_rewrite_of_non_def_line_is_not_blocked(agent, tmp_path):
    # arrange: a def plus an unrelated top-level assignment
    target = tmp_path / "cfg.py"
    target.write_text("def helper():\n    return 1\n\n\nTIMEOUT = 10\n")

    # act: rewrite only the assignment's value
    result = agent.run_tool("replace_lines", {
        "path": "cfg.py", "start": 5, "end": 5, "new_str": "TIMEOUT = 30\n",
    })

    # assert: no symbol loss, so the edit lands and the def survives
    assert "The edit was NOT applied" not in result
    written = target.read_text()
    assert "TIMEOUT = 30" in written
    assert "def helper():" in written


def test_deletion_and_symbol_loss_both_reported(agent, tmp_path):
    # arrange: a def and a regular line, both inside the replaced range
    target = tmp_path / "mixed.py"
    target.write_text("def helper(): pass\nordinary = 1\n")

    # act: pure deletion of the whole file body
    result = agent.run_tool("replace_lines", {
        "path": "mixed.py", "start": 1, "end": 2, "new_str": "",
    })

    # assert: both the deletion report and the symbol-loss note appear
    assert "deletes 2 line(s)" in result
    assert "permanently removes these top-level symbols" in result
    assert "helper" in result
    assert "The edit was NOT applied" in result


def test_non_python_file_skips_var_check(agent, tmp_path):
    # arrange: a markdown file has no top-level Python symbols
    target = tmp_path / "notes.md"
    target.write_text("# Title\n\nfirst paragraph\nsecond paragraph\n")

    # act: remove two lines
    result = agent.run_tool("replace_lines", {
        "path": "notes.md", "start": 3, "end": 4, "new_str": "",
    })

    # assert: no symbol-loss note (the .py-only var check is skipped)
    assert "top-level symbols" not in result


def test_no_symbol_loss_no_deletions_passes(agent, tmp_path):
    # arrange
    target = tmp_path / "mod.py"
    target.write_text("def f():\n    return 1\n")

    # act: pure insertion, nothing removed
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2,
        "new_str": "    log.debug('entering')\n    return 1\n",
    })

    # assert
    assert "The edit was NOT applied" not in result
    assert "log.debug" in target.read_text()
