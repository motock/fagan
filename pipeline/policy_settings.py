"""Read-only org/team policy settings from an operator-selected provider.

The provider is chosen only by ``PIPELINE_SETTINGS_PLUGIN``. A provider failure
fails closed: it is reported as an explicit error marker, never as ``{}``, so an
org policy cannot be silently dropped.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.plugin_loader import load_plugin, require_methods

logger = logging.getLogger("pipeline.policy_settings")

SETTINGS_SEAM = "settings"
PROVIDER_METHODS = ("policy_settings",)
SETTINGS_ERROR_KEY = "error"
SETTINGS_PROVIDER_ERROR = "settings_provider_error"

_LAYER_KEYS = ("org", "team")
_UNLOADED = object()
_PROVIDER: Any = _UNLOADED
_PROVIDER_LOCK = threading.Lock()


def _provider() -> Any:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is _UNLOADED:
            factory = load_plugin(SETTINGS_SEAM)
            if factory is None:
                _PROVIDER = None
            else:
                provider = factory()
                require_methods(provider, PROVIDER_METHODS, SETTINGS_SEAM)
                _PROVIDER = provider
        return _PROVIDER


def _fail_closed(error_type: str) -> dict:
    logger.warning(
        "policy settings provider failed; policy is restrictive/unknown",
        extra={"plugin_seam": SETTINGS_SEAM, "error_type": error_type},
    )
    return {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR}


def policy_settings(namespace: str, team: str | None) -> dict:
    """Return {"org": {...}, "team": {...}} (either sub-dict may be absent),
    or {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR} when the provider failed."""
    provider = _provider()
    if provider is None:
        return {}
    try:
        raw = provider.policy_settings(namespace, team)
    except Exception as exc:  # noqa: BLE001 - any provider failure fails closed
        return _fail_closed(type(exc).__name__)
    if not isinstance(raw, dict):
        return _fail_closed("non_dict_result")
    if any(key in raw and not isinstance(raw[key], dict) for key in _LAYER_KEYS):
        return _fail_closed("non_dict_layer")
    return {key: dict(raw[key]) for key in _LAYER_KEYS if key in raw}


def reset_policy_settings_cache() -> None:
    """Forget the cached provider (tests, reload)."""
    global _PROVIDER
    with _PROVIDER_LOCK:
        _PROVIDER = _UNLOADED
