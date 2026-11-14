"""Effective tenant settings for one (namespace, team, repo) context.

Reads the Store layers (deployment, org, team, team_repo) and hands them to the
pure resolver in ``pipeline.settings_resolve``. When ``PIPELINE_SETTINGS_PLUGIN``
selects a provider, its org and team layers replace the Store's for those two
scopes; the deployment and team_repo layers always come from the Store.

Never raises into the caller. A failure reading the Store resolves every key to
its safe value with source ``settings_error``; the warning names the exception
type only, never a value or a path.
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.live_ref import LiveRef
from pipeline.policy_settings import (
    SETTINGS_ERROR_KEY,
    policy_settings,
    provider_configured,
)
from pipeline.settings_registry import REGISTRY
from pipeline.settings_resolve import resolve

logger = logging.getLogger("pipeline.settings_store")

_store = LiveRef("_store")
STORE_READ_ERROR = "settings_store_read_failed"


def _store_layer(records, namespace: str, scope: str, scope_id: str, repo: str):
    record = records.get_settings(namespace, scope, scope_id, repo)
    if record is None:
        return None
    return {**record["values"], "allow_loosen": record["allow_loosen"]}


def _add(layers: dict, name: str, layer: Any) -> None:
    if layer is not None:
        layers[name] = layer


def _read_layers(namespace: str, *, org: str, team: str, repo: str) -> dict:
    """Return the layers mapping for the resolver. May raise on a read failure."""
    provider = provider_configured()
    layers: dict = {}
    if provider:
        policy = policy_settings(namespace, team)
        if SETTINGS_ERROR_KEY in policy:
            return policy
        layers.update(policy)
    records = _store.records()
    if not provider:
        if org:
            _add(layers, "org", _store_layer(records, namespace, "org", org, ""))
        _add(layers, "team", _store_layer(records, namespace, "team", team, ""))
    _add(layers, "deployment", _store_layer(records, namespace, "deployment", "", ""))
    # An empty repo names no repository, so there is no team_repo layer to read.
    if repo:
        _add(layers, "team_repo", _store_layer(records, namespace, "team_repo", team, repo))
    return layers


def _layers_or_error(namespace: str, *, org: str, team: str, repo: str) -> dict:
    try:
        return _read_layers(namespace, org=org, team=team, repo=repo)
    except Exception as exc:  # noqa: BLE001 - a settings read never raises into a gate
        logger.warning(
            "settings store read failed; resolving to safe values",
            extra={"error_type": type(exc).__name__},
        )
        return {SETTINGS_ERROR_KEY: STORE_READ_ERROR}


def read_layers(namespace: str, *, team: str, repo: str, org: str = "") -> dict:
    """The raw layers for one context, for a caller that resolves a key itself.

    Same sources as :func:`effective_setting`; a read failure returns the
    settings error marker instead of raising.
    """
    return _layers_or_error(namespace, org=org, team=team, repo=repo)


def _with_context(result: dict, team: str, repo: str) -> dict:
    return {**result, "team": team, "repo": repo}


def effective_setting(
    key: str,
    *,
    namespace: str,
    team: str,
    repo: str,
    org: str = "",
    request: Any = None,
) -> dict:
    """Return the resolved value and the layer that supplied it for one key.

    ``org`` is the plan's org; empty means the plan names none and the org
    layer is absent. ``request`` is an optional story-level request, refused
    unless the key allows it. Raises ``KeyError`` for an unknown key.
    """
    if key not in REGISTRY:
        raise KeyError(key)
    layers = _layers_or_error(namespace, org=org, team=team, repo=repo)
    return _with_context(resolve(key, layers, request=request), team, repo)


def effective_settings(namespace: str, team: str, repo: str, *, org: str = "") -> dict:
    """Return the resolved value and source for every registry key.

    Reads the Store once for all keys, so a read failure logs one warning.
    """
    layers = _layers_or_error(namespace, org=org, team=team, repo=repo)
    return {
        name: _with_context(resolve(name, layers), team, repo) for name in REGISTRY
    }
