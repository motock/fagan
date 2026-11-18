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


def layer_routing_kwargs(layer_routing: dict[str, Any] | None) -> dict[str, Any]:
    """``resolve_role``-style kwargs carrying ``layer_routing`` when it is set.

    Call sites that already forward a value to a shared resolver use this so an
    unset (``{}``/``None``) scope adds no keyword at all: the everyday call
    keeps its exact pre-existing shape and only a team that configured
    ``role_routing`` changes it.
    """
    return {"layer_routing": layer_routing} if layer_routing else {}


def _warn(exc: BaseException) -> None:
    """One value-free warning: the exception type only, never a manifest field."""
    logger.warning(
        "role_routing settings read failed; resolving without layer routing",
        extra={"error_type": type(exc).__name__},
    )


def _setting_for_context(context: Any) -> dict[str, Any]:
    """The resolved ``role_routing`` entry for one settings context (value+source)."""
    from .settings_store import effective_setting

    resolved = effective_setting(
        ROLE_ROUTING_KEY,
        namespace=context.namespace,
        team=context.team,
        repo=context.repo,
    )
    return resolved if isinstance(resolved, dict) else {}


def _configured_routing(resolved: Any) -> dict[str, Any] | None:
    """The routing mapping a resolution carries, or ``None`` when there is none.

    The source is compared, not the value's truthiness, so a deployment/team
    layer that never set the key can't leak a safe default mapping into role
    resolution; an unusable shape (e.g. the settings-error marker) is ``None``.
    """
    from .policy_settings import SETTINGS_ERROR_KEY
    from .settings_resolve import SAFE_DEFAULT_SOURCE

    if not isinstance(resolved, dict):
        return None
    if resolved.get("source") in (SAFE_DEFAULT_SOURCE, SETTINGS_ERROR_KEY):
        return None
    value = resolved.get("value")
    # An empty mapping routes nothing, so it is "no layer routing" too - a
    # caller then adds no provenance label for it.
    return value if isinstance(value, dict) and value else None


def _layer_routing_for_context(context: Any) -> dict[str, Any]:
    """The ``role_routing`` value for one settings context, or ``{}``.

    Returns ``{}`` when the Store answered with its safe default - the source
    is compared, not the value's truthiness, so a deployment/team layer that
    never set the key can't leak a default mapping into role resolution.
    """
    return _configured_routing(_setting_for_context(context)) or {}


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


def _context_for_plan(plan_name: str) -> Any | None:
    """The plan's settings context, or ``None`` after one warning.

    The manifest is loaded exactly as ``pipeline.ci.settings_for_plan`` loads
    it (an unknown or empty plan name yields the default context), so this
    stays consistent with every merge gate's scope.
    """
    try:
        from . import ci

        return ci.settings_for_plan(plan_name) if plan_name else ci.context_from_manifest({})
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return None


def layer_routing_for_plan(plan_name: str) -> dict[str, Any]:
    """Layer routing for the plan ``plan_name``'s stored manifest.

    Never raises; ``{}`` when the scope inherits the safe default, when the
    Store read fails, or when the manifest itself is unusable.
    """
    context = _context_for_plan(plan_name)
    if context is None:
        return {}
    try:
        return _layer_routing_for_context(context)
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}


def layer_routing_setting_for_plan(plan_name: str) -> dict[str, Any]:
    """The resolved ``role_routing`` entry (value **and** source) for a plan.

    Provenance needs the layer that supplied the routing, not just the mapping,
    so it can report the source as ``settings:<layer>``. ``{}`` when the scope
    inherits the safe default, when the Store read fails, or when the manifest
    is unusable - a caller then adds no label at all. Never raises.
    """
    context = _context_for_plan(plan_name)
    if context is None:
        return {}
    try:
        resolved = _setting_for_context(context)
        routing = _configured_routing(resolved)
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks dispatch
        _warn(exc)
        return {}
    if routing is None:
        return {}
    source = resolved.get("source")
    return {"value": routing, "source": source if isinstance(source, str) else ""}
