"""Default-deny tool policy for the remote MCP transport.

The bearer gate in ``pipeline.remote_transport`` proves who a caller is; it
does not decide what they may call. Every tool now decides through this policy:
the enforced tools' service paths ask ``authorize``, the read tools are scoped
to the plans a caller may see, and a tool named in neither set is refused before
its handler runs. A name in neither set is therefore a refusal, not a default
grant, so a new tool stays unreachable remotely until it is listed here.

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

Read tools are scoped to the plans a remote principal may see -- the plans it
owns or is a delegate of, per ``app.policy.can_view``:

* the plan-scoped read tools (``check_story_status``, ``get_effective_config``,
  ``get_story_diff``, ``list_decisions``, ``list_ready_stories``) refuse *before* the handler runs
  when their ``plan_name`` argument is missing, not a string, not a safe plan
  key (the same ``[A-Za-z0-9._-]`` rule the tools' own path building uses), or
  names a plan the caller may not see. The refusal is one fixed result,
  identical for a plan that does not exist and a plan the caller may not see,
  so the reply is no existence oracle; the refused call is recorded in the host
  chain as ``tool:call`` with outcome ``hidden``;
* ``list_plans`` still runs, and its stems are filtered afterwards to visible
  plans. A stem names a plan by the text before its first ``.``, so
  ``beta.manifest`` reports plan ``beta`` and is kept with it. A result shape
  this middleware cannot rewrite is withheld -- the hidden refusal stands in --
  rather than returned unfiltered. Structured content is read and rewritten
  through the field the SDK's ``CallToolResult`` model actually declares -- in
  either spelling, snake or camel case -- never through an attribute the model
  does not have;
* ``verify_audit`` is filtered the same way, and its ``host`` entry is dropped
  for every remote caller: the host chain records other principals'
  authentication attempts;
* ``check_usage`` stays host-level and unfiltered. It reports deployment-wide
  counters that belong to no plan, so there is nothing to scope;
* ``advance_all_plans`` runs for every plan, but its result keeps only the plans
  the caller may see. Each invisible plan's advance is refused by its own
  ``plan:dispatch`` check, so the filter hides names and does not gate work;
* ``save_plan`` is refused when its ``workspace`` argument is set. A server-side
  path from the network is never trusted. ``decompose_plan`` is refused outright:
  it is host-scoped, with no plan to authorize, and ``REMOTE_HOST_REFUSED`` lists
  it so the refusal is a recorded decision rather than an omission.
"""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from typing import Any

from app.identity import LOCAL_OPERATOR, Principal
from app.policy import can_view

from . import authorization, parsers
from .actor import ANONYMOUS, bind_principal, current_principal
from .audit import append_host_audit

logger = logging.getLogger("pipeline.remote_tool_policy")

#: Tools whose service path calls ``authorize`` itself, so the policy decides.
REMOTE_ENFORCED = frozenset(
    {
        "advance_all_plans",
        "advance_pipeline",
        "approve_merge",
        "approve_plan",
        "checkpoint",
        "checkpoint_story",
        "dispatch_story",
        "ingest_plan",
        "interrupt_story",
        "mark_story_done",
        "mark_story_in_progress",
        "patch_plan",
        "patch_story",
        "pause_plan",
        "record_security_audit",
        "reject_plan",
        "request_decision",
        "resume_plan",
        "review_story",
        "save_plan",
        "set_story_status",
    }
)

#: Read-only tools. A remote caller reads only the plans it owns or is a
#: delegate of: the plan-scoped tools below are gated on their ``plan_name``
#: argument and ``list_plans``/``verify_audit`` results are filtered to visible
#: plans. ``check_usage`` is host-level and unfiltered.
REMOTE_READ = frozenset(
    {
        "check_story_status",
        "check_usage",
        "get_effective_config",
        "get_story_diff",
        "list_decisions",
        "list_plans",
        "list_ready_stories",
        "verify_audit",
    }
)

_REFUSAL = "This tool is not available over the remote transport."

#: Host-scoped tools no remote principal may call. Kept out of REMOTE_ENFORCED
#: because their service path has no plan to authorize, and the only host-scoped
#: grant to a plan action (``plan:ingest``) is held by every authenticated
#: principal on an ownerless resource, so it cannot stand for the decision. They
#: are refused by default deny before the handler runs.
REMOTE_HOST_REFUSED = frozenset({"decompose_plan"})

#: Tools that accept a server-side ``workspace`` path, which a remote caller may
#: not supply. Refused when the argument is present and not ``None``.
WORKSPACE_REFUSED = frozenset({"save_plan"})

#: The single refusal a plan-scoped read returns for a plan the caller may not
#: see -- deliberately identical to the one for a plan that does not exist, so
#: the reply cannot be used to probe which plans are on disk.
_HIDDEN_PLAN = "This plan is not available to the authenticated principal."

#: Tools that read one named plan and are gated on their ``plan_name`` argument.
PLAN_SCOPED_READ = frozenset(
    {
        "check_story_status",
        "get_effective_config",
        "get_story_diff",
        "list_decisions",
        "list_ready_stories",
    }
)

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


def _audit_denial(name: object, principal: Principal, outcome: str = "denied") -> None:
    """Record a refused or hidden ``tools/call``; a failure must not change the decision."""
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
            append_host_audit(action="tool:call", outcome=outcome, detail=fields)
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


def _plan_name_argument(params: dict[str, Any]) -> object:
    """Return the ``plan_name`` argument of a ``tools/call``, or ``None``."""
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None
    return arguments.get("plan_name")


def _passes_workspace(params: dict[str, Any]) -> bool:
    """Return whether a ``tools/call`` names a non-``None`` ``workspace`` argument."""
    arguments = params.get("arguments")
    return isinstance(arguments, dict) and arguments.get("workspace") is not None


def _can_view_plan(principal: Principal, plan_name: object) -> bool:
    """Return whether *principal* may see *plan_name*, failing closed.

    ``app.policy.can_view`` fails closed inside its own body; this wrapper covers
    the rest, so a manifest that raises, a malformed resource or a non-string
    plan name hides the plan instead of failing the request. The warning names
    the principal subject only -- never the plan name or the exception text.
    """
    if not isinstance(plan_name, str) or not plan_name:
        return False
    # The name comes straight from the caller and the manifest lookup resolves
    # it to a path, so the same key rule the tools apply to their own plan paths
    # runs first: an unsafe name is hidden without touching the filesystem.
    if not parsers._KEY_RE.match(plan_name):
        return False
    try:
        manifest = authorization._load_manifest(plan_name)
        resource = authorization.resource_for(manifest, plan_name, None)
        return can_view(principal, resource)
    except Exception:  # noqa: BLE001 - a raising lookup must hide, not 500
        logger.warning(
            "plan visibility lookup failed; hiding plan principal=%s", principal.subject
        )
        return False


def _plan_of_stem(stem: str) -> str:
    """Return the plan a ``list_plans`` stem reports: the text before its first dot."""
    return stem.split(".", 1)[0]


def _visible_stems(principal: Principal, stems: list[str]) -> list[str]:
    """Return the stems whose plan *principal* may see, in their original order.

    Decisions are memoized per call only, so two stems of one plan cost one
    lookup and no decision outlives the request it was made for.
    """
    decisions: dict[str, bool] = {}
    kept: list[str] = []
    for stem in stems:
        plan_name = _plan_of_stem(stem)
        if plan_name not in decisions:
            decisions[plan_name] = _can_view_plan(principal, plan_name)
        if decisions[plan_name]:
            kept.append(stem)
    return kept


def _structured_body(result: Any) -> Any:
    """Return the structured content of a tool result, in either SDK spelling.

    The SDK's ``CallToolResult`` declares the field in snake case
    (``structured_content``) and only renames it to ``structuredContent`` on the
    wire, so the snake-case attribute is tried first. A dict result may carry
    either spelling, and a result object from a build that declares the alias
    as the field name is read the same way: whichever spelling holds a value
    wins, and a result carrying neither has no scoped body.
    """
    if not isinstance(result, dict):
        for attribute in ("structured_content", "structuredContent"):
            body = getattr(result, attribute, None)
            if body is not None:
                return body
        return None
    for key in ("structuredContent", "structured_content"):
        body = result.get(key)
        if body is not None:
            return body
    return None


def _structured_field(result: Any) -> str | None:
    """Return the structured-content field name *result*'s model declares.

    ``model_copy(update=...)`` writes by field name, so it has to be the name
    the model actually declares: a snake-case field would silently ignore an
    ``update`` keyed ``structuredContent``, and a model with no such field at
    all cannot carry a rewritten body. ``None`` means "not rewriteable".
    """
    fields = getattr(type(result), "model_fields", None)
    if not isinstance(fields, dict):
        return None
    for name in ("structured_content", "structuredContent"):
        if name in fields:
            return name
    return None


def _text_blocks(result: Any, texts: list[str]) -> list[Any]:
    """Return one text block per *texts*, matching the shape of *result*.

    A model result carries content *models*, and ``model_copy`` does not
    re-validate an update, so a plain dict there would leave the reply holding
    objects the SDK never produces. An existing block is copied with a new
    ``text`` when one is available (which keeps every other field and any
    annotations); otherwise a dict is used, which is what a dict result carries.
    """
    if isinstance(result, dict):
        return [{"type": "text", "text": text} for text in texts]
    existing = getattr(result, "content", None)
    blocks: list[Any] = []
    for index, text in enumerate(texts):
        block = existing[index] if isinstance(existing, list) and index < len(existing) else None
        copy_method = getattr(block, "model_copy", None)
        if callable(copy_method):
            blocks.append(copy_method(update={"text": text}))
        else:
            blocks.append({"type": "text", "text": text})
    return blocks


def _rebuilt(result: Any, body: Any, texts: list[str]) -> Any:
    """Return a copy of *result* carrying *body* and one text block per *texts*.

    One block per entry mirrors what the SDK builds for a list-returning tool,
    so a filtered reply keeps the shape the caller saw before. ``None`` means
    the result is a shape this middleware cannot rewrite; the caller then
    withholds it rather than passing unfiltered data through.
    """
    content = _text_blocks(result, texts)
    if isinstance(result, dict):
        copy = dict(result)
        key = "structuredContent" if "structuredContent" in copy else "structured_content"
        copy[key] = body
        copy["content"] = content
        return copy
    copy_method = getattr(result, "model_copy", None)
    field = _structured_field(result)
    if callable(copy_method) and field is not None:
        return copy_method(update={"content": content, field: body})
    return None


def _scope_stems(principal: Principal, result: Any) -> Any:
    """Return *result* with its ``list_plans`` stems filtered to visible plans."""
    body = _structured_body(result)
    stems = body.get("result") if isinstance(body, dict) else None
    if not isinstance(stems, list) or not all(isinstance(stem, str) for stem in stems):
        return None
    kept = _visible_stems(principal, stems)
    if kept == stems:
        return result
    return _rebuilt(result, {**body, "result": kept}, kept)


def _scope_audit(principal: Principal, result: Any) -> Any:
    """Return *result* with invisible plans and the ``host`` entry removed."""
    body = _structured_body(result)
    if not isinstance(body, dict):
        return None
    nested = body.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("plans"), dict):
        inner: dict[str, Any] = nested
    elif isinstance(body.get("plans"), dict):
        inner = body
    else:
        return None
    plans = {
        name: value
        for name, value in inner["plans"].items()
        if isinstance(name, str) and _can_view_plan(principal, name)
    }
    kept = {key: value for key, value in inner.items() if key not in ("plans", "host")}
    kept["plans"] = plans
    if inner is body and kept == body:
        return result
    scoped = {**body, "result": kept} if inner is nested else kept
    return _rebuilt(result, scoped, [json.dumps(scoped)])


def _refusal_result() -> dict[str, Any]:
    """The refusal for a tool the remote transport does not allow.

    The SDK's ``CallToolResult`` shape, returned before request validation
    runs; a protocol-level error would let some clients render the refusal as a
    success.
    """
    return {"content": [{"type": "text", "text": _REFUSAL}], "isError": True}


def _hidden_result() -> dict[str, Any]:
    """The single refusal for a hidden plan, identical however it was reached."""
    return {"content": [{"type": "text", "text": _HIDDEN_PLAN}], "isError": True}


def _or_hidden(scoped: Any) -> Any:
    """Return *scoped*, or the hidden refusal when the result could not be scoped."""
    return _hidden_result() if scoped is None else scoped


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

    An allowed read tool is then scoped to the plans the caller may see: a
    plan-scoped tool is refused *before* its handler runs, and the list-shaped
    results are filtered afterwards (see the module docstring).
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    principal = current_principal()
    params = ctx.params if isinstance(ctx.params, dict) else {}
    name = params.get("name")
    if not _is_local_identity(principal) and remote_tool_allowed(name):
        if name in WORKSPACE_REFUSED and _passes_workspace(params):
            _audit_denial(name, principal)
            return _refusal_result()
        if name in PLAN_SCOPED_READ:
            if not _can_view_plan(principal, _plan_name_argument(params)):
                _audit_denial(name, principal, outcome="hidden")
                return _hidden_result()
            return await call_next(ctx)
        result = await call_next(ctx)
        if name == "list_plans":
            return _or_hidden(_scope_stems(principal, result))
        # advance_all_plans still advances every plan: each invisible plan's
        # advance is refused by its own plan:dispatch check, so this filter only
        # hides the names it reports back.
        if name in ("verify_audit", "advance_all_plans"):
            return _or_hidden(_scope_audit(principal, result))
        return result
    _audit_denial(name, principal)
    return _refusal_result()
