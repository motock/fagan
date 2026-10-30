"""What a remote caller may see from ``get_effective_config``.

The effective config of a plan mixes two very different things:

* the plan's **role routing** -- which provider/model each role resolves to,
  which is what a remote caller needs in order to dispatch; and
* **host-private** detail -- the resolved environment (``env``,
  ``ignored_env_vars``) and filesystem locations (``sources``: the launchd
  plist, MCP config and role registry paths).

A caller arriving over the network must never receive the second group: it
leaks secrets and host layout to whoever holds a token or claims an identity
this host cannot vouch for. Trusted local callers -- stdio MCP and the
dashboard (``LOCAL_OPERATOR``), the launchd scheduler and workers (system
principals) -- keep the full result, because they already run as the operator
on this host. The gate is an allow-list of those trusted auth methods, so
every other principal (OIDC, the unauthenticated ``"none"``, and any future
transport) is denied by default.

Both entry points to that snapshot apply this policy: the MCP tool
``PipelineService.get_effective_config`` and the dashboard's ``/api/config``
(``app.dashboard_routes.effective_config``), which would otherwise hand the
same ``env``/``ignored_env_vars``/``sources`` to an OIDC-authenticated browser
behind the proxy.

Keeping this policy in a small module rather than in ``pipeline/service.py``
keeps the service file (already long, and touched by several later stories)
to a three-line delegation.
"""

from typing import Any

from app.identity import LOCAL_OPERATOR, SYSTEM_AUTH_METHOD

#: The refusal returned to a remote caller who named no plan. A remote caller
#: must say which plan it is asking about; there is no "the" plan for it.
MISSING_PLAN_NAME_ERROR = "plan_name is required for a remote caller"

#: The refusal returned when the config could not be read. It is deliberately
#: opaque: a remote caller learns that it got nothing, not why.
CONFIG_UNAVAILABLE_ERROR = "the effective config is not available"

#: The auth methods that identify a caller already running as the operator on
#: this host. This is an allow-list on purpose -- **deny by default**:
#:
#: * ``"shared-secret"`` is ``app.identity.LOCAL_OPERATOR``, bound by the stdio
#:   MCP server and the local dashboard. This is an explicit decision: the
#:   local operator may see the host environment because it *is* the host.
#: * ``"system"`` is ``app.identity.SYSTEM_AUTH_METHOD``, used by
#:   ``SYSTEM_SCHEDULER`` and ``SYSTEM_WORKER``; the daemon needs the full
#:   config to resolve providers and must keep working.
#:
#: Everything else is remote: OIDC, the unauthenticated ``"none"`` that the
#: dashboard binds for its chat pass-through, and any auth method a future
#: network transport introduces. Listing the trusted methods (rather than
#: naming the untrusted one) means a new transport cannot inherit the host
#: config merely by not being OIDC.
TRUSTED_LOCAL_AUTH_METHODS = frozenset({LOCAL_OPERATOR.auth_method, SYSTEM_AUTH_METHOD})


def is_network_principal(principal: Any) -> bool:
    """Return whether *principal* must be treated as a network caller.

    Fails closed: ``None``, a principal without a recognised trusted auth
    method, or an empty/unknown method is treated as remote.

    The principal type is taken as ``Any`` on purpose -- importing
    ``pipeline.actor`` here would create a circular import, since the service
    module imports this one. ``app.identity`` is a leaf and safe to import.
    """
    if principal is None:
        return True
    if principal is LOCAL_OPERATOR:
        return False
    return getattr(principal, "auth_method", None) not in TRUSTED_LOCAL_AUTH_METHODS


def remote_effective_config(result: dict, plan_name: str | None) -> dict:
    """Return the slice of *result* a remote caller may see.

    A remote caller needs the role routing and nothing else, so the response
    is built explicitly from the two permitted keys, never copied from
    *result* and pruned: an allow-list cannot leak a key that gets renamed or
    added later.

    A missing (``None`` or empty) *plan_name* is refused outright -- a remote
    caller must name the plan it is asking about. So is a result that is not
    a success: relaying ``{"ok": None, "roles": None}`` would be neither the
    success nor the documented refusal shape.
    """
    if not plan_name:
        return {"ok": False, "error": MISSING_PLAN_NAME_ERROR}
    if result.get("ok") is not True:
        return {"ok": False, "error": CONFIG_UNAVAILABLE_ERROR}
    return {"ok": True, "roles": result.get("roles", [])}


#: The refusal returned when a remote plan author names a server path. A
#: remote caller must describe its repository by URL; a path in the plan
#: (``repo_root``) or a ``workspace`` argument would point the server at a
#: directory of the caller's choosing on this host.
REMOTE_PLAN_PATH_ERROR = "a remote plan must name repo_url, not repo_root or a workspace"


def remote_plan_path_error(plan: dict, workspace: Any) -> str | None:
    """Return the refusal for a remote plan naming a server path, else ``None``.

    Presence decides, never truthiness: ``"repo_root": ""`` or ``None`` is
    still a caller-supplied path field, and an empty-string ``workspace`` is
    still a workspace argument, so both are refused. Refusing only truthy
    values would let a caller probe the field and leave the intent ambiguous.
    """
    if workspace is not None:
        return REMOTE_PLAN_PATH_ERROR
    if "repo_root" in plan:
        return REMOTE_PLAN_PATH_ERROR
    return None


def remote_dashboard_config(roles: Any) -> dict:
    """Return the dashboard ``/api/config`` payload for a network caller.

    The dashboard builds its snapshot as ``{"roles", "env",
    "ignored_env_vars", "sources"}``. A network caller may see ``roles`` --
    every entry already carries the provenance of the provider and model it
    resolved to -- and nothing about the host. Like
    :func:`remote_effective_config` this builds the response from the
    permitted key rather than copying and pruning, so a field added to the
    snapshot later is not disclosed by default.
    """
    return {"roles": roles}
