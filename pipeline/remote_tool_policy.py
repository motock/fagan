"""Default-deny tool policy for the remote MCP transport.

The bearer gate in ``pipeline.remote_transport`` proves who a caller is; it
does not decide what they may call. Only some tools reach a service path that
asks ``authorize`` -- the rest (``save_plan``, ``set_story_status``,
``record_security_audit`` ...) would run for any authenticated caller. So a
remote principal may call exactly the tools listed here, and every other tool
is refused before its handler runs.

The tool name decides, with one exception: a caller whose identity claims the
local operator's -- by singleton, authentication method or subject -- is refused
every tool, including the read-only ones. Identity can therefore only narrow
access, never widen it. A ``subject`` string is caller-mintable -- an OIDC
``sub`` claim can literally read ``local-operator`` -- so claiming that
identity costs a caller everything instead of exempting them, and a call that
resolves to the ``LOCAL_OPERATOR`` singleton (what ``current_principal`` hands
back when nothing is bound to the request) fails closed the same way. This
middleware is installed only by ``serve``, and ``serve`` refuses to serve with
anything but an ``OidcProvider``, so every ``tools/call`` that reaches it came
from the network. stdio and the local CLI never install the middleware, so their
behaviour is unchanged.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

from app.identity import LOCAL_OPERATOR, Principal

from .actor import ANONYMOUS, bind_principal, current_principal
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


def _is_local_identity(principal: Principal) -> bool:
    """Return whether *principal* claims the identity the local transport uses.

    Two checks, because either alone is brittle. The singleton identity check
    alone fails if a future refactor ever hands back a fresh ``Principal``
    carrying the local operator's values; the authentication method alone would
    not catch a caller who minted the local operator's subject. A call is
    refused when either matches.
    """
    return (
        principal.auth_method == LOCAL_OPERATOR.auth_method
        or principal.subject == LOCAL_OPERATOR.subject
    )


def _audit_denial(name: object, principal: Principal) -> None:
    """Record a denied ``tools/call``; a failure here must not change the decision."""
    # ``_append_at`` stamps ``actor`` from ``actor_id()``, which falls back to the
    # LOCAL_OPERATOR singleton when nothing is bound to the request. Recording a
    # denial as-is would therefore attribute a remote authorization failure to the
    # most privileged identity in the system -- permanently, in a tamper-evident
    # chain, and indistinguishably from a genuine local action. A principal that
    # claims the local identity is in the same position: the subject string is
    # caller-controlled, so it must not become the record's actor either. Such
    # denials are recorded as ``anonymous`` -- the transport's word for
    # "authenticated nobody" -- and the claimed subject is kept in the detail.
    unbound = principal is LOCAL_OPERATOR
    subject = "unbound" if unbound else principal.subject
    fields = {
        "tool": str(name)[:_AUDIT_FIELD_MAX],
        "subject": subject[:_AUDIT_FIELD_MAX],
    }
    attributed = not _is_local_identity(principal)
    try:
        with nullcontext() if attributed else bind_principal(ANONYMOUS):
            append_host_audit(action="tool:call", outcome="denied", detail=fields)
    except (OSError, TypeError, ValueError):
        # Auditing is best effort: the append can fail on the filesystem
        # (``OSError``) or on serialisation and chain parsing
        # (``TypeError``/``ValueError``), and a failed append must never turn a
        # clean denial into an internal error raised from a security
        # middleware, so the refusal below is returned regardless. The warning
        # names what was denied, so the hole in the audit trail is not silent.
        logger.warning(
            "host audit append failed for a denied remote tool call: tool=%s subject=%s",
            fields["tool"],
            fields["subject"],
        )


async def remote_tool_policy(ctx: Any, call_next: Any) -> Any:
    """MCP server middleware: refuse a remote ``tools/call`` outside the allowlist.

    The tool name decides, with one exception: a caller whose identity claims
    the local operator's -- by singleton, authentication method or subject -- is
    refused outright, every tool included. The identity check can therefore only
    remove access, never grant it. It is never an exemption: the policy runs
    behind the OIDC gate, so every caller here is remote, and a subject string is
    caller-mintable. A call that resolves to the local operator's identity is a
    denial for the same reason on a second axis -- that is what
    ``current_principal`` returns when nothing is bound to the request, and
    authorizing on the name alone would then let such a call reach a service
    path where the defaulted local operator is granted.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    principal = current_principal()
    params = ctx.params if isinstance(ctx.params, dict) else {}
    name = params.get("name")
    if not _is_local_identity(principal) and remote_tool_allowed(name):
        return await call_next(ctx)
    _audit_denial(name, principal)
    # The SDK's ``CallToolResult`` shape, returned before request validation
    # runs; a protocol-level error would let some clients render the refusal
    # as a success.
    return {"content": [{"type": "text", "text": _REFUSAL}], "isError": True}
