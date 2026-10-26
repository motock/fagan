"""Service-layer authorization: where the pipeline asks the policy for a decision.

ADR 0009 puts authorization in the service, not the transport, so the
dashboard, chat and MCP adapters cannot bypass it. ``app/policy.py`` supplies
the ``Authorizer`` and the fail-closed ``is_allowed`` wrapper; this module is
the single place the service asks, so every enforced operation shares one
semantics:

* enforcement is ON unless ``PIPELINE_POLICY_ENFORCE`` is set to an explicit
  disabling value -- the default is the restrictive one;
* a refusal is returned to the caller as ``{"ok": False, "error": ...}``, the
  shape every mutating service operation already returns;
* a refusal is recorded in the plan's notification sidecar, so an operator can
  see who was turned away and from what, before the audit stream exists;
* anything unexpected -- an unreadable manifest, a policy that raises -- lands
  denied, never allowed.
"""

from __future__ import annotations

import logging
import os

from app.policy import Action, Resource, default_authorizer, is_allowed

from .actor import current_principal

logger = logging.getLogger("pipeline")

#: Enforcement is on by default; only these values turn it off.
ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"
_DISABLING_VALUES = frozenset({"0", "false", "no", "off", "disabled"})

#: The team a plan belongs to when it declares none. One implicit team keeps
#: local mode a single-operator deployment; ADR 0009's tenant chain is only
#: populated once the shared control plane exists.
DEFAULT_TEAM = "local"

_warned_disabled = False


def enforcement_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return whether a refusal must be enforced.

    Only an explicit disabling value turns enforcement off; an unset, empty,
    unrecognized or misspelled value keeps it on, so a typo can never silently
    disable a security control.
    """
    env = os.environ if environ is None else environ
    return str(env.get(ENFORCE_ENV, "")).strip().lower() not in _DISABLING_VALUES


def resource_for(manifest: dict | None, plan_name: str, story_key: str | None) -> Resource:
    """Build the ADR 0009 scope chain for one operation."""
    data = manifest if isinstance(manifest, dict) else {}
    team = str(data.get("team") or DEFAULT_TEAM)
    return Resource(
        team=team,
        repo=str(data.get("repo_root") or ""),
        plan=plan_name,
        story=story_key or "",
    )


def _load_manifest(plan_name: str) -> dict:
    """Read the plan manifest, or an empty dict when it cannot be read."""
    try:
        from .store import FileStore

        return FileStore().get_manifest(plan_name)
    except Exception:  # noqa: BLE001 - an unreadable manifest must not allow
        return {}


def _record_denial(
    action: str, principal_subject: str, plan_name: str, story_key: str | None
) -> None:
    """Append an ``authorization_denied`` record to the plan's sidecar."""
    try:
        from .persistence import _notify_user

        _notify_user(
            plan_name,
            f"authorization denied: {action} by {principal_subject}",
            story_key=story_key,
            severity="warning",
            event="authorization_denied",
        )
    except Exception:  # noqa: BLE001 - recording a denial is best-effort
        logger.warning(
            "could not record authorization denial action=%s plan=%s", action, plan_name
        )


def authorize(
    action: Action | str,
    *,
    plan_name: str,
    story_key: str | None = None,
    manifest: dict | None = None,
) -> dict | None:
    """Return a refusal dict when *action* is denied, or ``None`` when allowed."""
    global _warned_disabled

    if not enforcement_enabled():
        if not _warned_disabled:
            _warned_disabled = True
            logger.warning(
                "%s disables policy enforcement; every action is allowed",
                ENFORCE_ENV,
            )
        return None

    action_label = action.value if isinstance(action, Action) else str(action)
    principal = current_principal()
    if manifest is None:
        manifest = _load_manifest(plan_name)
    allowed = is_allowed(
        default_authorizer(),
        principal,
        action,
        resource_for(manifest, plan_name, story_key),
    )
    if allowed:
        return None
    _record_denial(action_label, principal.subject, plan_name, story_key)
    logger.warning(
        "authorization denied action=%s principal=%s plan=%s story=%s",
        action_label,
        principal.subject,
        plan_name,
        story_key or "",
    )
    return {
        "ok": False,
        "error": (
            f"not authorized: {action_label} on plan {plan_name!r} "
            f"(principal {principal.subject!r})"
        ),
    }
