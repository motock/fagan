"""Default-deny tool policy for the remote MCP transport.

The bearer gate in ``pipeline.remote_transport`` proves who a caller is; it
does not decide what they may call. Only some tools reach a service path that
asks ``authorize`` -- the rest (``save_plan``, ``set_story_status``,
``record_security_audit`` ...) would run for any authenticated caller. So a
remote principal may call exactly the tools listed here, and every other tool
is refused before its handler runs. The local operator (stdio, or the shared
secret) is unaffected: this middleware is installed only by ``serve``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.identity import LOCAL_OPERATOR

from .actor import current_principal
from .audit import append_host_audit

logger = logging.getLogger("pipeline.remote_tool_policy")

#: Tools whose service path calls ``authorize`` itself, so the policy decides.
REMOTE_ENFORCED = frozenset({
    "approve_merge", "approve_plan", "dispatch_story", "ingest_plan",
    "interrupt_story", "mark_story_done", "patch_story", "pause_plan",
    "reject_plan", "resume_plan",
})

#: Read-only tools. They are not tenant-scoped yet, so any remote caller sees
#: every plan they list.
REMOTE_READ = frozenset({
    "check_story_status", "check_usage", "get_story_diff", "list_decisions",
    "list_plans", "list_ready_stories",
})

_REFUSAL = "This tool is not available over the remote transport."


def remote_tool_allowed(name: object) -> bool:
    """Return whether a remote principal may call tool *name*."""
    return isinstance(name, str) and (name in REMOTE_ENFORCED or name in REMOTE_READ)


async def remote_tool_policy(ctx: Any, call_next: Any) -> Any:
    """MCP server middleware: refuse a remote ``tools/call`` outside the allowlist."""
    if ctx.method != "tools/call":
        return await call_next(ctx)
    principal = current_principal()
    params = ctx.params if isinstance(ctx.params, dict) else {}
    name = params.get("name")
    if principal.subject == LOCAL_OPERATOR.subject or remote_tool_allowed(name):
        return await call_next(ctx)
    try:
        append_host_audit(
            action="tool:call",
            outcome="denied",
            detail={"tool": str(name)[:64], "subject": principal.subject},
        )
    except OSError:
        logger.warning("host audit append failed for a denied remote tool call")
    return {"content": [{"type": "text", "text": _REFUSAL}], "isError": True}
