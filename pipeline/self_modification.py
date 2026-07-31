"""Utilities for detecting when the MCP server's own source files are modified.

The pipeline MCP server is a long‑lived stdio child process of the claude CLI. It does not hot‑reload, so if its own source code changes during a merge the running instance continues executing the pre‑merge code until an operator reconnects or restarts it. This module provides small helpers that detect such modifications and construct a user‑facing notice.
"""

import subprocess
from pathlib import Path

# Repo‑relative paths to the MCP server's own source files.
MCP_SELF_SOURCE_FILES = ("pipeline/server.py", "app/pipeline_mcp_server.py")


def _mcp_self_source_touched(worktree: str, base_ref: str) -> list[str]:
    """Return a subset of :data:`MCP_SELF_SOURCE_FILES` that differ from ``base_ref``.

    Parameters
    ----------
    worktree:
        Path to the repository checkout. If falsy or not an existing directory,
        returns ``[]`` immediately.
    base_ref:
        Git ref to compare against (e.g. a branch name). The diff is computed
        between ``base_ref...HEAD``.

    Returns
    -------
    list[str]
        Paths that appear in the diff, ordered as they appear in
        :data:`MCP_SELF_SOURCE_FILES` and each at most once.
    """
    if not worktree or not Path(worktree).is_dir():
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    # Build a set of stripped lines for exact matching.
    changed = {line.strip() for line in proc.stdout.splitlines()}
    result: list[str] = []
    for path in MCP_SELF_SOURCE_FILES:
        if path in changed:
            result.append(path)
    return result


def _mcp_restart_notice(touched: list[str]) -> str:
    """Construct a user‑facing notice when the MCP server's source was touched.

    The returned string contains the literal substring ``/mcp reconnect`` and
    every path in *touched*.  It also explains that the running instance does
    not hot‑reload and must be restarted before further actions.
    """
    paths = ", ".join(touched)
    return (
        f"This merge changed the MCP server's own source ({paths}). The running pipeline MCP server is a long-lived stdio child of the claude CLI and does NOT hot-reload - it is still executing pre-merge code. Run `/mcp reconnect` in Claude Code (or restart it) before dispatching, reviewing, or merging any further story."
    )

__all__ = ["MCP_SELF_SOURCE_FILES", "_mcp_self_source_touched", "_mcp_restart_notice"]
