"""Minimal MCP server exposing only the `checkpoint` tool.

Registered with OpenHands for local dispatch (see backend.py's
OllamaDriver._write_mcp_config) instead of the full pipeline_mcp_server.py.

Why a separate server: a dispatched agent's task is one story. The full
pipeline server's other ~18 tools (dispatch_story, advance_pipeline,
approve_merge, mark_story_done, ...) are orchestration-level and the agent
has no legitimate reason to call them. Exposing them anyway is a real
privilege-escalation surface for a less reliable local model, and in
practice also degraded tool selection - found via real end-to-end testing,
where an agent given ~20 tools instead of its normal handful tried to call
a hallucinated "create_file" tool instead of its own file-edit tool.

Reuses pipeline_mcp_server's PLAN_DIR config and checkpoint logic directly,
so it needs the same env vars (PLAN_DIR at minimum) - backend.py passes them
through when writing the MCP config.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import pipeline_mcp_server as p  # noqa: E402

mcp = FastMCP("pipeline-checkpoint-only")


@mcp.tool()
def checkpoint(
    plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "",
) -> dict:
    """
    Record a durable checkpoint for a dispatched agent's progress.

    Commits any uncommitted work in the story's worktree as a WIP commit and
    appends an entry to the story's journal. Call this after completing each
    idempotent step of a story so a killed agent can resume from the last
    checkpoint instead of starting over.
    """
    return p._checkpoint_impl(plan_name, story_key, step, summary, next_hint)


if __name__ == "__main__":
    mcp.run()
