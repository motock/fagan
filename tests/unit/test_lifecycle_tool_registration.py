"""ENT-LC-00: the lifecycle MCP tools are registered from a single TOOLS tuple.

These tests pin the two identities the registration loop must preserve:

* ``p.<name> is server_tools_lifecycle.<name>`` — the server module re-exports
  the plain function, not the ``Tool`` wrapper ``mcp.tool()`` returns.
* ``mcp._tool_manager._tools[<name>].fn is server_tools_lifecycle.<name>`` —
  the registered object wraps that same function.

They also pin the tuple's length/order and the ``pipeline/server.py`` line cap
that motivated the refactor.
"""

from pathlib import Path

import pipeline.server as p
from pipeline import server_tools_lifecycle as lifecycle

EXPECTED_TOOL_NAMES = [
    "request_decision",
    "list_decisions",
    "review_story",
    "advance_pipeline",
    "approve_merge",
    "pause_plan",
    "resume_plan",
    "advance_all_plans",
    "record_security_audit",
    "approve_plan",
    "reject_plan",
]

SERVER_PY = Path(__file__).resolve().parents[2] / "pipeline" / "server.py"


def test_tools_tuple_has_nine_entries_in_original_order():
    # Name is historical (ENT-LC-00 pinned nine); ENT-LC-02b appended the two
    # plan-approval tools, so the pin now covers eleven. The assertion is
    # unchanged in strength: exact length plus exact order.
    assert len(lifecycle.TOOLS) == 11
    assert [tool.__name__ for tool in lifecycle.TOOLS] == EXPECTED_TOOL_NAMES


def test_every_tool_is_registered_with_the_module_function_as_fn():
    registered = p.mcp._tool_manager._tools
    for name in EXPECTED_TOOL_NAMES:
        assert name in registered, f"{name} is not registered on the MCP server"
        assert registered[name].fn is getattr(lifecycle, name)


def test_server_module_reexports_the_plain_functions():
    for name in EXPECTED_TOOL_NAMES:
        assert getattr(p, name) is getattr(lifecycle, name)


def test_server_py_stays_within_line_cap():
    assert len(SERVER_PY.read_text(encoding="utf-8").splitlines()) <= 994
