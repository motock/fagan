"""The autonomy, auto_merge and triage_mode tenant settings keys.

Three behaviours that used to live only in environment variables become
tenant settings keys: the autonomy level (``dry-run``/``gated``/``full``), the
auto-merge switch and the triage mode (``shadow``/``execute``). Each is a
ceiling, so a layer below may only tighten the value in force; the safe value
is the most restrictive one.

The environment names stay alive: these keys declare no retired environment
name, so the env guard does not refuse them and the seed ignores them.
"""

import pytest

from pipeline import settings_registry, settings_seed, settings_tools
from pipeline.settings_env_guard import refuse_retired_env, retired_env_present
from pipeline.settings_registry import LAYERS, REGISTRY
from pipeline.settings_resolve import SAFE_DEFAULT_SOURCE, resolve

AUTONOMY = "autonomy"
AUTO_MERGE = "auto_merge"
TRIAGE_MODE = "triage_mode"

#: (key, type, safe value) of the three newly registered keys.
NEW_KEYS = (
    (AUTONOMY, "enum", "gated"),
    (AUTO_MERGE, "bool", True),
    (TRIAGE_MODE, "enum", "shadow"),
)

#: The autonomy levels, tightest first.
AUTONOMY_LEVELS = ("dry-run", "gated", "full")

#: The environment names these behaviours live in. They stay in use, so no key
#: may retire them.
LEGACY_ENV = {
    "PIPELINE_AUTONOMY": "full",
    "PIPELINE_AUTO_MERGE": "0",
    "PIPELINE_TRIAGE_MODE": "execute",
}


# --- the keys themselves ----------------------------------------------------


@pytest.mark.parametrize("name,type_name,safe_value", NEW_KEYS)
def test_new_key_is_registered_as_a_ceiling(name, type_name, safe_value):
    assert name in REGISTRY
    key = REGISTRY[name]
    assert key.name == name
    assert key.type == type_name
    assert key.kind == "ceiling"
    assert key.safe_value == safe_value


@pytest.mark.parametrize("name,type_name,safe_value", NEW_KEYS)
def test_new_key_may_be_set_by_every_layer(name, type_name, safe_value):
    assert set(LAYERS) <= set(REGISTRY[name].allowed_layers)


@pytest.mark.parametrize("name,type_name,safe_value", NEW_KEYS)
def test_new_key_retires_no_environment_name(name, type_name, safe_value):
    assert REGISTRY[name].legacy_env == ""


@pytest.mark.parametrize(
    "name,accepted,rejected",
    [
        (AUTONOMY, ("dry-run", "gated", "full"), ("yolo", "GATED", "", "full ")),
        (AUTO_MERGE, (True, False), ("yes", "0", 1, None)),
        (TRIAGE_MODE, ("shadow", "execute"), ("run", "Shadow", "", "execute ")),
    ],
)
def test_new_key_accepts_only_its_own_values(name, accepted, rejected):
    key = REGISTRY[name]
    for value in accepted:
        assert key.accepts(value) is True
    for value in rejected:
        assert key.accepts(value) is False


# --- resolution -------------------------------------------------------------


@pytest.mark.parametrize("name,type_name,safe_value", NEW_KEYS)
def test_no_layer_resolves_to_the_safe_default(name, type_name, safe_value):
    result = resolve(name, {})
    assert result["value"] == safe_value
    assert result["source"] == SAFE_DEFAULT_SOURCE
    assert result["lock_rejections"] == []
    assert result["invalid_layers"] == []


@pytest.mark.parametrize("name,type_name,safe_value", NEW_KEYS)
def test_a_layer_without_the_key_leaves_the_safe_default_in_force(
    name, type_name, safe_value
):
    result = resolve(name, {"deployment": {"unrelated": 1}})
    assert result["value"] == safe_value
    assert result["source"] == SAFE_DEFAULT_SOURCE


def test_autonomy_team_layer_tightens_the_deployment_value():
    result = resolve(
        AUTONOMY, {"deployment": {AUTONOMY: "full"}, "team": {AUTONOMY: "gated"}}
    )
    assert (result["value"], result["source"]) == ("gated", "team")
    assert result["lock_rejections"] == []


def test_autonomy_tightest_level_survives_a_looser_team_value():
    result = resolve(
        AUTONOMY, {"deployment": {AUTONOMY: "dry-run"}, "team": {AUTONOMY: "gated"}}
    )
    assert (result["value"], result["source"]) == ("dry-run", "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": "gated", "by": "deployment"}
    ]


def test_autonomy_team_may_not_loosen_the_deployment_value():
    result = resolve(
        AUTONOMY, {"deployment": {AUTONOMY: "gated"}, "team": {AUTONOMY: "full"}}
    )
    assert (result["value"], result["source"]) == ("gated", "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": "full", "by": "deployment"}
    ]


def test_autonomy_deployment_may_opt_into_the_looser_team_value():
    layers = {
        "deployment": {AUTONOMY: "gated", "allow_loosen": [AUTONOMY]},
        "team": {AUTONOMY: "full"},
    }
    result = resolve(AUTONOMY, layers)
    assert (result["value"], result["source"]) == ("full", "team")
    assert result["lock_rejections"] == []


def test_auto_merge_team_layer_turns_the_switch_off():
    result = resolve(
        AUTO_MERGE, {"deployment": {AUTO_MERGE: True}, "team": {AUTO_MERGE: False}}
    )
    assert (result["value"], result["source"]) == (False, "team")
    assert result["lock_rejections"] == []


def test_auto_merge_team_layer_may_not_turn_the_switch_on():
    result = resolve(
        AUTO_MERGE, {"deployment": {AUTO_MERGE: False}, "team": {AUTO_MERGE: True}}
    )
    assert (result["value"], result["source"]) == (False, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": True, "by": "deployment"}
    ]


def test_triage_mode_team_layer_shadows_the_execution():
    result = resolve(
        TRIAGE_MODE,
        {"deployment": {TRIAGE_MODE: "execute"}, "team": {TRIAGE_MODE: "shadow"}},
    )
    assert (result["value"], result["source"]) == ("shadow", "team")
    assert result["lock_rejections"] == []


def test_triage_mode_team_layer_may_not_leave_shadow():
    result = resolve(
        TRIAGE_MODE,
        {"deployment": {TRIAGE_MODE: "shadow"}, "team": {TRIAGE_MODE: "execute"}},
    )
    assert (result["value"], result["source"]) == ("shadow", "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": "execute", "by": "deployment"}
    ]


@pytest.mark.parametrize(
    "name,bad_value,safe_value",
    [
        (AUTONOMY, "yolo", "gated"),
        (AUTO_MERGE, "yes", True),
        (TRIAGE_MODE, "run", "shadow"),
    ],
)
def test_an_invalid_value_is_reported_and_the_safe_value_stands(
    name, bad_value, safe_value
):
    result = resolve(name, {"deployment": {name: bad_value}})
    assert result["invalid_layers"] == ["deployment"]
    assert result["value"] == safe_value
    assert result["source"] == SAFE_DEFAULT_SOURCE


@pytest.mark.parametrize(
    "name,bad_value,good_value",
    [
        (AUTONOMY, "yolo", "full"),
        (AUTO_MERGE, "yes", True),
        (TRIAGE_MODE, "run", "execute"),
    ],
)
def test_an_invalid_team_value_leaves_the_deployment_value_in_force(
    name, bad_value, good_value
):
    layers = {"deployment": {name: good_value}, "team": {name: bad_value}}
    result = resolve(name, layers)
    assert result["invalid_layers"] == ["team"]
    assert (result["value"], result["source"]) == (good_value, "deployment")


# --- the tighter functions --------------------------------------------------


@pytest.mark.parametrize("first", AUTONOMY_LEVELS)
@pytest.mark.parametrize("second", AUTONOMY_LEVELS)
def test_the_autonomy_tighter_function_keeps_the_lower_rank(first, second):
    expected = (
        first
        if AUTONOMY_LEVELS.index(first) <= AUTONOMY_LEVELS.index(second)
        else second
    )
    tighter = settings_registry._lower_autonomy_is_tighter
    assert tighter(first, second) == expected
    assert tighter(second, first) == expected
    assert REGISTRY[AUTONOMY].tighter(first, second) == expected


def test_the_off_tighter_function_keeps_off():
    tighter = settings_registry._off_is_tighter
    assert tighter(True, True) is True
    assert tighter(True, False) is False
    assert tighter(False, True) is False
    assert tighter(False, False) is False
    assert REGISTRY[AUTO_MERGE].tighter(True, False) is False
    assert REGISTRY[AUTO_MERGE].tighter(False, True) is False


def test_the_shadow_tighter_function_keeps_shadow():
    tighter = settings_registry._shadow_is_tighter
    assert tighter("shadow", "shadow") == "shadow"
    assert tighter("shadow", "execute") == "shadow"
    assert tighter("execute", "shadow") == "shadow"
    assert tighter("execute", "execute") == "execute"
    assert REGISTRY[TRIAGE_MODE].tighter("shadow", "execute") == "shadow"
    assert REGISTRY[TRIAGE_MODE].tighter("execute", "shadow") == "shadow"


# --- the environment names stay in use --------------------------------------


def test_env_guard_does_not_retire_the_new_keys():
    assert retired_env_present(dict(LEGACY_ENV)) == []
    assert refuse_retired_env(dict(LEGACY_ENV)) is None


def test_env_guard_still_retires_a_name_that_a_key_declares():
    findings = retired_env_present({"PIPELINE_MERGE_CI_GATE": "1"})
    assert findings == [
        {"name": "PIPELINE_MERGE_CI_GATE", "replacement": "merge_ci_gate"}
    ]


def test_seed_ignores_the_new_environment_names(monkeypatch):
    assert {AUTONOMY, AUTO_MERGE, TRIAGE_MODE} <= set(REGISTRY)
    monkeypatch.setattr(
        settings_tools,
        "get_settings",
        lambda *args, **kwargs: {"ok": True, "record": None},
    )
    report = settings_seed.seed_from_env(dict(LEGACY_ENV))
    assert report["ok"] is True
    assert report["written"] == []
    assert report["planned"] == []
    assert report["unchanged"] == []
    assert report["skipped"] == []
