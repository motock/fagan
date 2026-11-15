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


def _accepts_role_routing(value: Any) -> bool:
    """True when *value* pins each role to a provider and model declared in the
    live model registry (``PIPELINE_MODEL_REGISTRY_PATH`` or the repo default).

    Every deny path returns False: a malformed value, an undeclared provider or
    model, or a registry that cannot be read.
    """
    # Imported here: app.* must not load at module import (see test_preflight).
    from app.role_registry import RoleRegistryError, load_registry

    if not isinstance(value, dict):
        return False
    try:
        providers = load_registry().get("providers", {})
    except (OSError, RoleRegistryError):
        return False
    if not isinstance(providers, dict):
        return False
    for role, pin in value.items():
        if not isinstance(role, str) or not role or not isinstance(pin, dict):
            return False
        if set(pin) != {"provider", "model"}:
            return False
        provider, model = pin["provider"], pin["model"]
        if not isinstance(provider, str) or not isinstance(model, str):
            return False
        provider_cfg = providers.get(provider)
        if not isinstance(provider_cfg, dict):
            return False
        if model not in provider_cfg.get("models", {}):
            return False
    return True


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
        if self.type == "routing":
            return _accepts_role_routing(value)
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
        SettingKey(
            name="role_routing",
            type="routing",
            kind="pref",
            safe_value={},
            # Every layer except the host-only deployment layer, which LAYERS lists first.
            allowed_layers=LAYERS[1:],
            # A new key has no retired environment name to refuse.
            legacy_env="",
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


def _plugin_keys(
    core: Mapping[str, SettingKey], environ: Mapping[str, str]
) -> list[SettingKey]:
    """Return the validated operator-plugin keys, or raise PluginError."""
    if str(environ.get(_PLURAL_PLUGIN_ENV, "")).strip():
        # The loader would ignore this variable; refuse rather than run without it.
        raise PluginError(f"{_PLURAL_PLUGIN_ENV} is not supported; use {_PLUGIN_ENV}")
    require_allowlist(SETTINGS_KEYS_SEAM, environ)
    factory = load_plugin(SETTINGS_KEYS_SEAM, environ)
    if factory is None:
        return []
    try:
        entries = list(factory())
    except Exception as exc:  # any plugin failure fails closed
        raise PluginError("settings key plugin failed while building its keys") from exc
    known = dict(core)
    keys = []
    for index, entry in enumerate(entries):
        key = _validate_plugin_key(index, entry, known)
        known[key.name] = key
        keys.append(key)
    return keys


class _Registry(dict):
    """A plain dict of core keys that merges operator plugin keys on first read.

    Callers read and patch it as they always have. The plugin keys are validated
    and merged once; a plugin that fails validation makes every read raise, so
    the registry never runs without the keys the operator configured.
    """

    def __init__(self, core: Mapping[str, SettingKey]) -> None:
        super().__init__(core)
        self._core = core
        self._merged = False
        self._plugin_names: tuple[str, ...] = ()

    def _ensure(self) -> None:
        if self._merged:
            return
        keys = _plugin_keys(self._core, os.environ)
        for key in keys:
            dict.__setitem__(self, key.name, key)
        self._plugin_names = tuple(key.name for key in keys)
        self._merged = True

    def _reset(self) -> None:
        """Drop the plugin keys so the next read re-reads the environment."""
        for name in self._plugin_names:
            dict.pop(self, name, None)
        self._plugin_names = ()
        self._merged = False

    def __getitem__(self, name: str) -> SettingKey:
        self._ensure()
        return super().__getitem__(name)

    def __contains__(self, name: object) -> bool:
        self._ensure()
        return super().__contains__(name)

    def __iter__(self) -> Iterator[str]:
        self._ensure()
        return super().__iter__()

    def __len__(self) -> int:
        self._ensure()
        return super().__len__()

    def get(self, name: str, default: Any = None) -> Any:
        self._ensure()
        return super().get(name, default)

    def keys(self):
        self._ensure()
        return super().keys()

    def values(self):
        self._ensure()
        return super().values()

    def items(self):
        self._ensure()
        return super().items()


REGISTRY: dict[str, SettingKey] = _Registry(CORE_REGISTRY)
