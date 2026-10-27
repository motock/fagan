"""Default-deny tool policy for the remote MCP transport.

The bearer gate in ``pipeline.remote_transport`` proves who a caller is; it
does not decide what they may call. Only some tools reach a service path that
asks ``authorize`` -- the rest (``save_plan``, ``set_story_status``,
``record_security_audit`` ...) would run for any authenticated caller. So a
remote principal may call exactly the tools listed here, and every other tool
is refused before its handler runs.

Authorization is decided on the tool name and nothing else. This middleware is
installed only by ``serve``, and ``serve`` refuses to serve with anything but
an ``OidcProvider``, so every ``tools/call`` that reaches it came from the
network. A ``subject`` string is caller-mintable -- an OIDC ``sub`` claim can
literally read ``local-operator`` -- so it carries no authority. The
``LOCAL_OPERATOR`` singleton itself is not a remote principal either: it is
what ``current_principal`` hands back when nothing is bound to the request, so
a call that resolves to it is a denial rather than an unrestricted local call.
stdio and the local CLI never install the middleware, so their behaviour is
unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from app.identity import LOCAL_OPERATOR, Principal

from .actor import current_principal
from .audit import append_host_audit

logger = logging.getLogger("pipeline.remote_tool_policy")

#: Tools whose service path calls ``authorize`` itself, so the policy decides.
REMOTE_ENFORCED = frozenset(
    {
        "approve_merge",
        "approve_plan",
        "dispatch_story",
        "ingest_plan",
        "interrupt_story",
        "mark_story_done",
        "patch_story",
        "pause_plan",
        "reject_plan",
        "resume_plan",
    }
)

#: Read-only tools. They are not tenant-scoped yet, so any remote caller sees
#: every plan they list.
REMOTE_READ = frozenset(
    {
        "check_story_status",
        "check_usage",
        "get_story_diff",
        "list_decisions",
        "list_plans",
        "list_ready_stories",
    }
)

_REFUSAL = "This tool is not available over the remote transport."

#: Longest value written for the caller-influenced audit fields.
_AUDIT_FIELD_MAX = 64


def remote_tool_allowed(name: object) -> bool:
    """Return whether a remote principal may call tool *name*."""
    return isinstance(name, str) and (name in REMOTE_ENFORCED or name in REMOTE_READ)


def _audit_denial(name: object, principal: Principal) -> None:
    """Record a denied ``tools/call``; a failure here must not change the decision."""
    # ``current_principal`` falls back to the LOCAL_OPERATOR singleton when
    # nothing is bound to the request. That call carries no attested remote
    # identity, so the record says ``unbound`` rather than blaming the local
    # operator for a call the local operator never makes (it never installs
    # this middleware).
    subject = "unbound" if principal is LOCAL_OPERATOR else principal.subject
    try:
        append_host_audit(
            action="tool:call",
            outcome="denied",
            detail={
                "tool": str(name)[:_AUDIT_FIELD_MAX],
                "subject": subject[:_AUDIT_FIELD_MAX],
            },
        )
    except (OSError, TypeError, ValueError):
        # Auditing is best effort: the append can fail on the filesystem
        # (``OSError``) or on serialisation and chain parsing
        # (``TypeError``/``ValueError``), and a failed append must never turn a
        # clean denial into an internal error raised from a security
        # middleware, so the refusal below is returned regardless.
        logger.warning("host audit append failed for a denied remote tool call")


async def remote_tool_policy(ctx: Any, call_next: Any) -> Any:
    """MCP server middleware: refuse a remote ``tools/call`` outside the allowlist.

    Authorization is decided on the tool name only. It is never decided on the
    principal's ``subject``: the policy runs behind the OIDC gate, so every
    caller here is remote, and a subject string is caller-mintable. A call that
    resolves to the ``LOCAL_OPERATOR`` singleton is a denial as well -- that is
    what ``current_principal`` returns when nothing is bound to the request, and
    authorizing on the name alone would then let such a call reach a service
    path where the defaulted local operator is granted.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    principal = current_principal()
    params = ctx.params if isinstance(ctx.params, dict) else {}
    name = params.get("name")
    if principal is not LOCAL_OPERATOR and remote_tool_allowed(name):
        return await call_next(ctx)
    _audit_denial(name, principal)
    # The SDK's ``CallToolResult`` shape, returned before request validation
    # runs; a protocol-level error would let some clients render the refusal
    # as a success.
    return {"content": [{"type": "text", "text": _REFUSAL}], "isError": True}
