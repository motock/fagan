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
``LOCAL_OPERATOR`` identity itself is not a remote principal either: it is what
``current_principal`` hands back when nothing is bound to the request, and it is
the only identity the local transport authenticates, so a call that resolves to
it is a denial rather than an unrestricted local call. stdio and the local CLI
never install the middleware, so their behaviour is unchanged.
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
    """Return whether *principal* is the identity the local transport authenticates.

    Two checks, because either alone is brittle. Identity alone fails if a future
    refactor ever hands back a fresh ``Principal`` carrying the local operator's
    values instead of the singleton; the authentication method alone would also
    catch an unrelated credential of the same kind. Together, a call is refused
    unless its principal is neither the shared-secret identity's method nor the
    local operator's subject.
    """
    return (
        principal.auth_method == LOCAL_OPERATOR.auth_method
        or principal.subject == LOCAL_OPERATOR.subject
    )


def _audit_denial(name: object, principal: Principal) -> None:
    """Record a denied ``tools/call``; a failure here must not change the decision."""
    # ``current_principal`` falls back to the LOCAL_OPERATOR singleton when
    # nothing is bound to the request, and ``_append_at`` stamps ``actor`` from
    # ``actor_id()`` -- the same fallback. Recording the refusal as-is would
    # therefore attribute a remote authorization failure to the most privileged
    # identity in the system, permanently and indistinguishably from a genuine
    # local action. Rebinding here makes the chain say ``anonymous``, the
    # transport's word for "authenticated nobody".
    unbound = principal is LOCAL_OPERATOR
    subject = "unbound" if unbound else principal.subject
    fields = {
        "tool": str(name)[:_AUDIT_FIELD_MAX],
        "subject": subject[:_AUDIT_FIELD_MAX],
    }
    try:
        with bind_principal(ANONYMOUS) if unbound else nullcontext():
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

    Authorization is decided on the tool name only. It is never decided on the
    principal's ``subject``: the policy runs behind the OIDC gate, so every
    caller here is remote, and a subject string is caller-mintable. A call that
    resolves to the local operator's identity is a denial as well -- that is
    what ``current_principal`` returns when nothing is bound to the request, and
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
