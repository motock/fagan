"""pipeline.server_tools_lifecycle must import cold, and its tools must still register.

The module used to decorate its tools with ``@mcp.tool()`` where ``mcp`` was a lazy
reference to pipeline.server's FastMCP instance. Decorating resolved the server at
import time, and pipeline.server imports this module, so a cold
``import pipeline.server_tools_lifecycle`` re-entered it half-initialized and died
with an ImportError. The tools are now plain functions that pipeline.server registers.
tests/unit/conftest.py pre-imports pipeline.server, which hides the failure
in-process, so every probe here runs in a subprocess.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TOOL_NAMES = [
    "request_decision",
    "list_decisions",
    "review_story",
    "advance_pipeline",
    "approve_merge",
    "pause_plan",
    "resume_plan",
    "advance_all_plans",
]


def _probe(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_server_tools_lifecycle_imports_cold_in_a_fresh_interpreter():
    result = _probe("import pipeline.server_tools_lifecycle")
    assert result.returncode == 0, result.stderr


def test_importing_server_tools_lifecycle_does_not_load_the_server():
    result = _probe("import sys, pipeline.server_tools_lifecycle; print('pipeline.server' in sys.modules)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_server_registers_the_lifecycle_tool_and_re_exports_the_same_function(name):
    result = _probe(
        "import pipeline.server as p, pipeline.server_tools_lifecycle as t; "
        f"fn = getattr(p, {name!r}); "
        f"print(fn is getattr(t, {name!r}), p.mcp._tool_manager._tools[{name!r}].fn is fn)"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True True"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_lifecycle_first_then_server_registers_the_same_tool(name):
    result = _probe(
        "import pipeline.server_tools_lifecycle as t, pipeline.server as p; "
        f"print(p.mcp._tool_manager._tools[{name!r}].fn is getattr(t, {name!r}))"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
