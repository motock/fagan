"""Registry of the tenant settings keys and how each one combines across layers.

Declarative only: ``pipeline.settings_resolve`` applies the layer rules.
``kind`` is one of ``lock`` (read only from the deployment layer),
``ceiling`` (a layer may only tighten, unless the layer above opts in with
``allow_loosen``) or ``pref`` (the most specific valid layer wins).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.plugin_loader import (
    PluginError,
    load_plugin,
    plugin_env_name,
    require_allowlist,
)
from pipeline.test_scope import TEST_SCOPE_VALUES

LAYERS = ("deployment", "org", "team", "team_repo")
REQUEST_LAYER = "request"

_ALL_LAYERS = LAYERS
_TEST_SCOPE_LAYERS = LAYERS + (REQUEST_LAYER,)

SETTINGS_KEYS_SEAM = "settings_keys"
_PLUGIN_ENV = plugin_env_name(SETTINGS_KEYS_SEAM)
_PLURAL_PLUGIN_ENV = _PLUGIN_ENV + "S"
_KINDS = ("lock", "ceiling", "pref")
_TYPES = ("bool", "int", "enum")
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _on_is_tighter(a: bool, b: bool) -> bool:
    return a or b


def _lower_is_tighter(a: int, b: int) -> int:
    return min(a, b)


def _test_scope_tighter(a: str, b: str) -> str:
    return min(a, b, key=TEST_SCOPE_VALUES.index)


@dataclass(frozen=True)
class SettingKey:
    name: str
    type: str
    kind: str
    safe_value: Any
    allowed_layers: tuple[str, ...]
    legacy_env: str
    tighter: Callable[[Any, Any], Any] | None = None
    choices: tuple[str, ...] = ()

    def accepts(self, value: Any) -> bool:
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "int":
            # Timeouts and worker counts are positive; 0 and negatives are invalid.
            return isinstance(value, int) and not isinstance(value, bool) and value >= 1
        return isinstance(value, str) and value in self.choices


CORE_REGISTRY: dict[str, SettingKey] = {
    key.name: key
    for key in (
        SettingKey(
            name="merge_ci_gate",
            type="bool",
            kind="ceiling",
            safe_value=True,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_CI_GATE",
            tighter=_on_is_tighter,
        ),
        SettingKey(
            name="merge_build_gate",
            type="bool",
            kind="ceiling",
            safe_value=True,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_BUILD_GATE",
            tighter=_on_is_tighter,
        ),
        SettingKey(
            name="test_scope",
            type="enum",
            kind="ceiling",
            safe_value=TEST_SCOPE_VALUES[0],
            allowed_layers=_TEST_SCOPE_LAYERS,
            legacy_env="PIPELINE_TEST_SCOPE",
            tighter=_test_scope_tighter,
            choices=TEST_SCOPE_VALUES,
        ),
        SettingKey(
            name="merge_ci_timeout_s",
            type="int",
            kind="pref",
            safe_value=900,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_CI_TIMEOUT",
        ),
        SettingKey(
            name="merge_readiness_timeout_s",
            type="int",
            kind="pref",
            safe_value=180,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_READINESS_TIMEOUT",
        ),
        SettingKey(
            name="rework_on_ci_fail",
            type="bool",
            kind="pref",
            safe_value=False,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_REWORK_ON_CI_FAIL",
        ),
        SettingKey(
            name="reverify_full_suite",
            type="bool",
            kind="pref",
            safe_value=False,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_REVERIFY_FULL_SUITE",
        ),
        SettingKey(
            name="grade_xdist_workers",
            type="int",
            kind="ceiling",
            safe_value=1,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_GRADE_XDIST_WORKERS",
            tighter=_lower_is_tighter,
        ),
    )
}


def _is_layer_subset(layers: Any) -> bool:
    return isinstance(layers, tuple) and all(layer in LAYERS for layer in layers)


def _choices_match_type(key: SettingKey) -> bool:
    if key.type != "enum":
        return key.choices == ()
    return (
        isinstance(key.choices, tuple)
        and len(key.choices) > 0
        and all(isinstance(choice, str) for choice in key.choices)
    )


def _validate_plugin_key(
    index: int, entry: Any, known: Mapping[str, SettingKey]
) -> SettingKey:
    """Return *entry* if it is a well-formed new key, else raise PluginError.

    Messages name the rule and the entry's position, never its values: a
    plugin entry may carry identifiers the operator did not intend to log.
    """
    where = f"settings key plugin entry {index}"
    if not isinstance(entry, SettingKey):
        raise PluginError(f"{where} is not a SettingKey")
    if not isinstance(entry.name, str) or not _NAME_RE.fullmatch(entry.name):
        raise PluginError(f"{where} has an invalid name")
    if entry.name in known:
        raise PluginError(f"{where} collides with an existing key")
    if entry.type not in _TYPES:
        raise PluginError(f"{where} has an unknown type")
    if entry.kind not in _KINDS:
        raise PluginError(f"{where} has an unknown kind")
    if not _is_layer_subset(entry.allowed_layers):
        raise PluginError(f"{where} lists a layer outside LAYERS")
    if not _choices_match_type(entry):
        raise PluginError(f"{where} has choices that do not match its type")
    if entry.kind == "ceiling":
        if not callable(entry.tighter):
            raise PluginError(f"{where} is a ceiling without a tighter function")
    elif entry.tighter is not None:
        raise PluginError(f"{where} sets tighter but is not a ceiling")
    if entry.legacy_env != "":
        raise PluginError(f"{where} must not declare a legacy environment name")
    if not entry.accepts(entry.safe_value):
        raise PluginError(f"{where} has a default its own type rejects")
    return entry


def _merged_registry(
    core: Mapping[str, SettingKey], environ: Mapping[str, str]
) -> dict[str, SettingKey]:
    """Return the core keys plus any operator-plugin keys, or raise PluginError."""
    if str(environ.get(_PLURAL_PLUGIN_ENV, "")).strip():
        # The loader would ignore this variable; refuse rather than run without it.
        raise PluginError(f"{_PLURAL_PLUGIN_ENV} is not supported; use {_PLUGIN_ENV}")
    merged = dict(core)
    require_allowlist(SETTINGS_KEYS_SEAM, environ)
    factory = load_plugin(SETTINGS_KEYS_SEAM, environ)
    if factory is None:
        return merged
    try:
        entries = list(factory())
    except Exception as exc:  # any plugin failure fails closed
        raise PluginError("settings key plugin failed while building its keys") from exc
    for index, entry in enumerate(entries):
        key = _validate_plugin_key(index, entry, merged)
        merged[key.name] = key
    return merged


class _Registry(Mapping):
    """Core keys plus operator plugin keys, merged on first access.

    Read-only: it offers no way to add or replace an entry. A plugin that fails
    validation makes every access raise, so the registry never runs without the
    keys the operator configured.
    """

    def __init__(self, core: Mapping[str, SettingKey]) -> None:
        self._core = core
        self._view: Mapping[str, SettingKey] | None = None

    def _loaded(self) -> Mapping[str, SettingKey]:
        if self._view is None:
            self._view = MappingProxyType(_merged_registry(self._core, os.environ))
        return self._view

    def _reset(self) -> None:
        """Forget the merged view so the next access re-reads the environment."""
        self._view = None

    def __getitem__(self, name: str) -> SettingKey:
        return self._loaded()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._loaded())

    def __len__(self) -> int:
        return len(self._loaded())


REGISTRY: Mapping[str, SettingKey] = _Registry(CORE_REGISTRY)
