"""Resolve one tenant settings key across the policy layers.

Pure: no I/O, no environment reads, no logging. Result shape matches
``pipeline.test_scope.resolve_test_scope``. A ceiling layer's value stands only
if it is tighter-or-equal to the value already in force, or if the layer that
set that value lists the key in its ``allow_loosen``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from pipeline.policy_settings import SETTINGS_ERROR_KEY
from pipeline.settings_registry import LAYERS, REGISTRY, REQUEST_LAYER, SettingKey

SAFE_DEFAULT_SOURCE = "safe_default"
SETTINGS_ERROR_SOURCE = "settings_error"


def _result(value, source, lock_rejections, invalid_layers):
    return {
        "value": value,
        "source": source,
        "lock_rejections": lock_rejections,
        "invalid_layers": invalid_layers,
    }


def _refused(setting: SettingKey, request):
    rejections = []
    if request is not None and request != setting.safe_value:
        rejections.append(
            {"layer": REQUEST_LAYER, "requested": request, "by": SETTINGS_ERROR_SOURCE}
        )
    return _result(setting.safe_value, SETTINGS_ERROR_SOURCE, rejections, [])


def _may_loosen(setting: SettingKey, source: str, layer_maps: dict) -> bool:
    layer = layer_maps.get(source)
    if layer is None:
        return False
    allowed = layer.get("allow_loosen")
    return isinstance(allowed, (list, tuple)) and setting.name in allowed


def _allows(setting: SettingKey, name: str, request_layer: str) -> bool:
    layer = REQUEST_LAYER if name == request_layer else name
    return layer in setting.allowed_layers


def resolve(
    key: str, layers: Mapping, *, request: Any = None, request_layer: str = REQUEST_LAYER
) -> dict:
    """Resolve *key* across *layers*, then the optional *request*.

    *request_layer* names the request in the result (a caller may call it
    ``plan`` or ``story``); it is still checked against the key's request slot.
    """
    setting = REGISTRY[key]
    if not isinstance(layers, Mapping) or SETTINGS_ERROR_KEY in layers:
        return _refused(setting, request)

    layer_maps = {
        name: layers[name] for name in LAYERS if isinstance(layers.get(name), Mapping)
    }
    offered = [
        (name, layer_maps[name].get(setting.name))
        for name in LAYERS
        if name in layer_maps
    ]
    if request is not None:
        offered.append((request_layer, request))

    invalid_layers = []
    lock_rejections = []
    current: tuple[Any, str] | None = None
    for name, value in offered:
        if value is None:
            continue
        if not _allows(setting, name, request_layer) or not setting.accepts(value):
            invalid_layers.append(name)
            continue
        if setting.kind == "ceiling" and current is not None:
            # A request that agrees with the policy in force changes nothing,
            # so the policy layer keeps the source.
            if name == request_layer and value == current[0]:
                continue
            looser = cast(Callable[[Any, Any], Any], setting.tighter)(value, current[0]) != value
            if looser and not _may_loosen(setting, current[1], layer_maps):
                lock_rejections.append(
                    {"layer": name, "requested": value, "by": current[1]}
                )
                continue
        current = (value, name)

    if current is None:
        return _result(
            setting.safe_value, SAFE_DEFAULT_SOURCE, lock_rejections, invalid_layers
        )
    return _result(current[0], current[1], lock_rejections, invalid_layers)
