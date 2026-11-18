"""The team's ``role_routing`` setting, resolved for a manifest or a plan.

Role resolution takes layer routing as its OWN argument - never merged into a
plan's ``role_config`` - because ``resolve_role``'s pairing rule honours a
layer model only when the layer provider is the one that won. Every runtime
``resolve_role`` call on the execution path therefore needs this value for its
own (namespace, team, repo) scope.

A settings read must never break dispatch or review, so every failure here -
including a plan whose manifest cannot be loaded - degrades to "no layer
routing" (``{}``) with one value-free WARNING, and nothing is cached: a Store
that recovers on the next tick takes effect on the next call.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("pipeline")

ROLE_ROUTING_KEY = "role_routing"


def _warn(exc: BaseException) -> None:
    """One value-free warning: the exception type only, never a manifest field."""
    logger.warning(
        "role_routing settings read failed; resolving without layer routing",
        extra={"error_type": type(exc).__name__},
    )


def _layer_routing_for_context(context: Any) -> dict[str, Any]:
    """The ``role_routing`` value for one settings context, or ``{}``.

    Returns ``{}`` when the Store answered with its safe default - the source
    is compared, not the value's truthiness, so a deployment/team layer that
    never set the key can't leak a default mapping into role resolution.
    """
    from .settings_resolve import SAFE_DEFAULT_SOURCE
    from .settings_store import effective_setting

    resolved = effective_setting(
        ROLE_ROUTING_KEY,
        namespace=context.namespace,
        team=context.team,
        repo=context.repo,
    )
    try:
        if resolved["source"] == SAFE_DEFAULT_SOURCE:
            return {}
        value = resolved["value"]
    except (KeyError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def layer_routing_for_manifest(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Layer routing for ``manifest``'s (namespace, team, repo) scope.

    ``{}`` when the scope inherits the safe default, when the Store read fails,
    or when the manifest itself is unusable - never raises.
    """
    try:
        # Lazy: pipeline.ci imports the settings resolver, as merge gating does
        # (same pattern as pipeline/detached_grade.py's xdist_workers).
        from .ci import context_from_manifest

        context = context_from_manifest(manifest or {})
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}
    try:
        return _layer_routing_for_context(context)
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}


def layer_routing_for_plan(plan_name: str) -> dict[str, Any]:
    """Layer routing for the plan ``plan_name``'s stored manifest.

    The manifest is loaded exactly as ``pipeline.ci.settings_for_plan`` loads
    it (an unknown or empty plan name yields the default context), so this
    stays consistent with every merge gate's scope. Never raises.
    """
    try:
        from . import ci

        context = ci.settings_for_plan(plan_name) if plan_name else ci.context_from_manifest({})
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}
    try:
        return _layer_routing_for_context(context)
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}
