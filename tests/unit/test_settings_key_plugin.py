"""Operator plugins may register extra settings keys (PIPELINE_SETTINGS_KEYS_PLUGIN).

The registry validates plugin keys on first access and fails closed: any
invalid key raises PluginError on that access instead of running without the
extension's keys. With no plugin configured the registry is the core registry.
"""

import sys
import types

import pytest

from app.plugin_loader import PluginError
from pipeline.settings_registry import CORE_REGISTRY, LAYERS, REGISTRY, SettingKey
from pipeline.settings_resolve import resolve

PLUGIN_ENV = "PIPELINE_SETTINGS_KEYS_PLUGIN"
PLURAL_ENV = "PIPELINE_SETTINGS_KEYS_PLUGINS"
ALLOWLIST_ENV = "PIPELINE_PLUGIN_MODULES"
MODULE = "fake_settings_keys_plugin"


def _probe(**overrides) -> SettingKey:
    fields = {
        "name": "sf_probe_flag",
        "type": "bool",
        "kind": "pref",
        "safe_value": False,
        "allowed_layers": ("deployment", "team_repo"),
        "legacy_env": "",
    }
    fields.update(overrides)
    return SettingKey(**fields)


def _loosen_cap(a: int, b: int) -> int:
    return min(a, b)


@pytest.fixture
def fresh_registry(monkeypatch):
    """Activate a plugin for one test; the registry re-reads env on next access."""
    monkeypatch.delenv(PLUGIN_ENV, raising=False)
    monkeypatch.delenv(PLURAL_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    REGISTRY._reset()
    yield REGISTRY
    REGISTRY._reset()


@pytest.fixture
def plugin_keys(monkeypatch, fresh_registry):
    """Return a setter that installs a plugin whose ``keys()`` returns *keys*."""

    def install(keys):
        module = types.ModuleType(MODULE)

        def keys_factory():
            return keys() if callable(keys) else keys

        keys_factory.__module__ = MODULE
        module.keys = keys_factory
        monkeypatch.setitem(sys.modules, MODULE, module)
        monkeypatch.setenv(PLUGIN_ENV, f"{MODULE}:keys")
        monkeypatch.setenv(ALLOWLIST_ENV, MODULE)
        REGISTRY._reset()

    return install


# --- unset plugin: the unchanged path -------------------------------------


def test_unset_plugin_leaves_registry_identical_to_core(fresh_registry):
    assert list(REGISTRY) == list(CORE_REGISTRY)
    assert dict(REGISTRY) == CORE_REGISTRY


def test_unset_plugin_resolves_exactly_as_before(fresh_registry):
    result = resolve("merge_ci_gate", {"deployment": {"merge_ci_gate": False}})
    assert result == {
        "value": False,
        "source": "deployment",
        "lock_rejections": [],
        "invalid_layers": [],
    }


def test_blank_plugin_variable_is_treated_as_unset(monkeypatch, fresh_registry):
    monkeypatch.setenv(PLUGIN_ENV, "   ")
    assert dict(REGISTRY) == CORE_REGISTRY


# --- valid plugin keys -----------------------------------------------------


def test_valid_plugin_key_is_merged_into_the_registry(plugin_keys):
    plugin_keys([_probe()])
    assert set(REGISTRY) == set(CORE_REGISTRY) | {"sf_probe_flag"}
    assert REGISTRY["sf_probe_flag"].type == "bool"


def test_valid_plugin_key_resolves_through_the_normal_resolver(plugin_keys):
    plugin_keys([_probe()])
    result = resolve("sf_probe_flag", {"deployment": {"sf_probe_flag": True}})
    assert (result["value"], result["source"]) == (True, "deployment")


def test_plugin_ceiling_key_uses_its_tighter_function(plugin_keys):
    cap = SettingKey(
        name="sf_probe_cap",
        type="int",
        kind="ceiling",
        safe_value=10,
        allowed_layers=("deployment", "team_repo"),
        legacy_env="",
        tighter=_loosen_cap,
    )
    plugin_keys([cap])
    layers = {
        "deployment": {"sf_probe_cap": 5},
        "team_repo": {"sf_probe_cap": 8},
    }
    result = resolve("sf_probe_cap", layers)
    assert (result["value"], result["source"]) == (5, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team_repo", "requested": 8, "by": "deployment"}
    ]


def test_valid_plugin_enum_key_accepts_its_choices(plugin_keys):
    mode = SettingKey(
        name="sf_probe_mode",
        type="enum",
        kind="pref",
        safe_value="alpha",
        allowed_layers=("deployment",),
        legacy_env="",
        choices=("alpha", "beta"),
    )
    plugin_keys([mode])
    assert (
        resolve("sf_probe_mode", {"deployment": {"sf_probe_mode": "beta"}})["value"]
        == "beta"
    )


@pytest.mark.parametrize("name", ["a", "a" * 64])
def test_name_length_boundaries_are_accepted(plugin_keys, name):
    plugin_keys([_probe(name=name)])
    assert name in REGISTRY


# --- refusals: each makes the registry access raise PluginError -----------


def _assert_refused(plugin_keys, keys):
    plugin_keys(keys)
    with pytest.raises(PluginError):
        list(REGISTRY)


@pytest.mark.parametrize(
    "bad_name",
    ["", "Bad", "1leading_digit", "has-dash", "a" * 65, "trailing\n", "sp ace"],
)
def test_invalid_name_refuses_activation(plugin_keys, bad_name):
    _assert_refused(plugin_keys, [_probe(name=bad_name)])


def test_collision_with_core_key_refuses_activation(plugin_keys):
    _assert_refused(plugin_keys, [_probe(name="merge_ci_gate")])


def test_duplicate_plugin_keys_refuse_activation(plugin_keys):
    _assert_refused(plugin_keys, [_probe(), _probe()])


@pytest.mark.parametrize("bad_kind", ["lock_all", "", "Ceiling"])
def test_unknown_kind_refuses_activation(plugin_keys, bad_kind):
    _assert_refused(plugin_keys, [_probe(kind=bad_kind)])


@pytest.mark.parametrize("bad_type", ["float", "str", ""])
def test_unknown_type_refuses_activation(plugin_keys, bad_type):
    _assert_refused(plugin_keys, [_probe(type=bad_type)])


@pytest.mark.parametrize(
    "bad_default",
    ["yes", 1, None],
)
def test_default_the_bool_key_rejects_refuses_activation(plugin_keys, bad_default):
    _assert_refused(plugin_keys, [_probe(safe_value=bad_default)])


def test_int_default_below_one_refuses_activation(plugin_keys):
    probe = _probe(type="int", safe_value=0)
    _assert_refused(plugin_keys, [probe])


def test_enum_default_outside_choices_refuses_activation(plugin_keys):
    probe = _probe(
        type="enum",
        safe_value="gamma",
        choices=("alpha", "beta"),
        allowed_layers=("deployment",),
    )
    _assert_refused(plugin_keys, [probe])


@pytest.mark.parametrize(
    "bad_layers",
    [("request",), ("deployment", "galaxy"), ("deployment", "")],
)
def test_layer_outside_layers_refuses_activation(plugin_keys, bad_layers):
    _assert_refused(plugin_keys, [_probe(allowed_layers=bad_layers)])


def test_layers_given_as_a_bare_string_refuse_activation(plugin_keys):
    _assert_refused(plugin_keys, [_probe(allowed_layers="deployment")])


def test_ceiling_without_tighter_refuses_activation(plugin_keys):
    cap = _probe(kind="ceiling", type="int", safe_value=3)
    _assert_refused(plugin_keys, [cap])


def test_tighter_on_a_non_ceiling_key_refuses_activation(plugin_keys):
    _assert_refused(plugin_keys, [_probe(tighter=_loosen_cap)])


def test_non_empty_legacy_env_refuses_activation(plugin_keys):
    _assert_refused(plugin_keys, [_probe(legacy_env="PIPELINE_SF_PROBE")])


def test_entry_that_is_not_a_setting_key_refuses_activation(plugin_keys):
    _assert_refused(plugin_keys, [{"name": "sf_probe_flag"}])


def test_one_bad_key_among_valid_ones_refuses_the_whole_set(plugin_keys):
    _assert_refused(plugin_keys, [_probe(), _probe(name="Bad")])


def test_plugin_factory_that_raises_refuses_activation(plugin_keys):
    def explode():
        raise RuntimeError("boom")

    _assert_refused(plugin_keys, explode)


def test_plugin_that_returns_a_non_iterable_refuses_activation(plugin_keys):
    _assert_refused(plugin_keys, 5)


def test_refused_registry_raises_on_membership_and_resolve(plugin_keys):
    plugin_keys([_probe(name="merge_ci_gate")])
    with pytest.raises(PluginError):
        "merge_ci_gate" in REGISTRY  # noqa: B015 - the access itself must raise
    with pytest.raises(PluginError):
        resolve("merge_ci_gate", {})


def test_refusal_is_repeated_on_every_access_not_cached(plugin_keys):
    plugin_keys([_probe(name="Bad")])
    with pytest.raises(PluginError):
        list(REGISTRY)
    with pytest.raises(PluginError):
        list(REGISTRY)


def test_plugin_without_allowlist_is_refused(monkeypatch, fresh_registry):
    module = types.ModuleType(MODULE)
    module.keys = lambda: [_probe()]
    module.keys.__module__ = MODULE
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv(PLUGIN_ENV, f"{MODULE}:keys")
    REGISTRY._reset()
    with pytest.raises(PluginError):
        list(REGISTRY)


def test_plural_plugin_variable_is_refused_rather_than_ignored(
    monkeypatch, fresh_registry
):
    monkeypatch.setenv(PLURAL_ENV, f"{MODULE}:keys")
    REGISTRY._reset()
    with pytest.raises(PluginError):
        list(REGISTRY)


# --- registry shape --------------------------------------------------------


def test_registry_accepts_item_patching_as_the_core_dict_does(
    monkeypatch, fresh_registry
):
    probe = _probe(name="sf_patched_probe")
    monkeypatch.setitem(REGISTRY, "sf_patched_probe", probe)
    assert REGISTRY["sf_patched_probe"] is probe
    monkeypatch.undo()
    assert "sf_patched_probe" not in REGISTRY


def test_plugin_factory_runs_once_and_its_keys_merge_once(plugin_keys):
    calls = []

    def keys():
        calls.append(1)
        return [_probe()]

    plugin_keys(keys)
    list(REGISTRY)
    len(REGISTRY)
    REGISTRY["sf_probe_flag"]
    assert calls == [1]
    assert list(REGISTRY).count("sf_probe_flag") == 1


def test_plugin_layers_stay_within_the_policy_layer_names(plugin_keys):
    plugin_keys([_probe(allowed_layers=LAYERS)])
    assert REGISTRY["sf_probe_flag"].allowed_layers == LAYERS
