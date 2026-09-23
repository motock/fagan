"""MCPHYG-1: ``check_story_status`` must be registered as an MCP tool.

``CLAUDE.md`` and ``REFERENCE.md`` document ``check_story_status`` as callable
via ``mcp__pipeline__check_story_status``, but the module-level import in
``pipeline/server.py`` was never decorated with ``@mcp.tool()``, so the tool
was absent from the MCP tool manager's registry.

Two assertions:

* registration: the tool name appears in ``mcp._tool_manager.list_tools()``
  exactly once (mirrors ``test_list_ready_stories_migration.py``'s
  ``test_list_ready_stories_is_a_public_mcp_tool``).
* behaviour: a single smoke call still returns the real status dict for a
  dispatched-and-finished story. The full behavioural suite lives in
  ``test_pipeline_mcp_server_advance_orchestration_1.py``; this is only a
  guard that registration did not replace the function with a wrapper.
"""
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    plan_dir,
)


def test_check_story_status_is_a_public_mcp_tool():
    """The tool is registered with the MCP tool manager, exactly once."""
    names = [t.name for t in p.mcp._tool_manager.list_tools()]
    assert "check_story_status" in names, (
        "check_story_status must be decorated with @mcp.tool() to be callable "
        "as mcp__pipeline__check_story_status"
    )
    assert names.count("check_story_status") == 1, (
        "check_story_status must be registered exactly once; found "
        f"{names.count('check_story_status')} registrations"
    )


def test_check_story_status_still_returns_correct_output(
    request, tmp_path, monkeypatch,
):
    """Registration must not wrap the function: a real call still routes a
    finished story through its tests and lands on ``tests_passed``."""
    plans = request.getfixturevalue("plan_dir")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n")
    _write_manifest(plans, "smoke1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("smoke1", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plans, "smoke1")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"
