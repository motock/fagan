"""Resolve ``autonomy``, ``auto_merge`` and ``triage_mode`` for one plan.

These three behaviours used to live only in environment variables. They are now
tenant settings keys, but the environment variables stay: at the point of use a
plan's value is resolved with the environment value standing in for the
deployment layer, so a lower layer may only tighten it.

* No plan context: the environment value, exactly as before.
* The Store layers cannot be read: the most restrictive value, with one warning
  that names the setting and never its value.
* A plan whose repo is not a settings key (a local path): no repo layer is read; the team, deployment and environment values still apply.
* Otherwise: the environment value fills the deployment layer when that layer
  does not set the key, then the registry resolves the key. An environment value
  the key does not accept is not injected, so a raw invalid value survives only
  when no layer sets the key either.
"""

from __future__ import annotations

import logging

from pipeline.policy_settings import SETTINGS_ERROR_KEY
from pipeline.settings_registry import REGISTRY
from pipeline.settings_resolve import resolve
from pipeline.settings_store import read_layers
from pipeline.store_settings import is_settings_repo

logger = logging.getLogger("pipeline.autonomy_settings")

RESTRICTIVE = {
    "autonomy": "dry-run",
    "auto_merge": False,
    "triage_mode": "shadow",
    "overlord_precedent_limit": 0,
}

_WARNING = "autonomy setting unreadable; using the restrictive value"


def _warn(key: str, error: Exception | None = None) -> None:
    """Report an unreadable setting without leaking its value."""
    extra: dict = {"setting_key": key}
    if error is not None:
        extra["error_type"] = type(error).__name__
    logger.warning(_WARNING, extra=extra)


def effective(key: str, legacy, context) -> object:
    """Return the value in force for *key* for the plan named by *context*.

    *context* is any object with ``namespace``, ``team`` and ``repo`` (see
    ``pipeline.ci.SettingsContext``); ``None`` means the caller has no plan, so
    *legacy* — the environment value — is returned untouched.
    """
    if context is None:
        return legacy
    try:
        # A plan named only by a local path has no team_repo layer: no settings
        # record can carry a path as its repo, so read the layers above it.
        repo = context.repo if is_settings_repo(context.repo) else ""
        layers = read_layers(context.namespace, team=context.team, repo=repo)
        if SETTINGS_ERROR_KEY in layers:
            _warn(key)
            return RESTRICTIVE[key]
        deployment = dict(layers.get("deployment") or {})
        if deployment.get(key) is None and REGISTRY[key].accepts(legacy):
            # The environment value stands in for the deployment layer. Copy the
            # layer so a shared mapping is never mutated for the next caller.
            deployment[key] = legacy
            layers = {**layers, "deployment": deployment}
        result = resolve(key, layers)
        if result["source"] == "safe_default":
            # Nothing set the key and the environment value was not accepted:
            # keep today's raw value rather than inventing a default.
            return legacy
        return result["value"]
    except Exception as exc:  # noqa: BLE001 - a settings read never breaks a gate
        _warn(key, exc)
        return RESTRICTIVE[key]


def effective_autonomy(legacy: str, context) -> str:
    """Return the autonomy mode in force, ``legacy`` being the environment value."""
    return effective("autonomy", str(legacy).lower(), context)


def effective_auto_merge(legacy: bool, context) -> bool:
    """Return whether auto-merge is in force, ``legacy`` being the env value."""
    return effective("auto_merge", legacy, context)


def effective_triage_mode(legacy: str, context) -> str:
    """Return the triage mode in force, ``legacy`` being the environment value."""
    return effective("triage_mode", legacy, context)
