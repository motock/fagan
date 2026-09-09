"""Tests for the ``effective_in_process`` key on resolve_env_var layer dicts.

Story: make the layers list distinguish "this value is in effect" from
"this value exists in a file but does not reach this process".

Contract under test (pipeline.config_provenance.resolve_env_var):
  * every layer dict gains ONE key, ``effective_in_process`` (bool);
  * True for the layer that supplied the effective value -- the layer whose
    name matches ``source`` (and for ``code_default`` when
    ``source == "code_default"``);
  * False for layers that are present in a file but did not supply it;
  * across the layers of one resolved var, exactly one layer is True;
  * NOTHING else changes: ``effective``, ``source``, ``conflict``,
    ``masked``, the resolution order, the top-level result keys, and
    EnvVarSpec are all frozen.

The key does not exist yet on this branch, so these tests are intentionally
RED (KeyError on 'effective_in_process') until the implementation lands.
"""
from dataclasses import fields as dataclass_fields

import pytest

import pipeline.config_provenance as mod

TOP_LEVEL_KEYS = {
    "name",
    "effective",
    "source",
    "restart_required",
    "conflict",
    "masked",
    "layers",
}
LAYER_KEYS = {"layer", "value", "restart_required", "effective_in_process"}

# (id, kwargs, expected_source, expected_effective, expected_conflict)
SCENARIOS = [
    ("process_env_only",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {}, "mcp_env": {}},
     "process_env", "60", False),
    ("plist_only_not_in_process",
     {"default": "40", "environ": {}, "plist_env": {"X": "80"}, "mcp_env": {}},
     "code_default", "40", False),
    ("mcp_only_not_in_process",
     {"default": "40", "environ": {}, "plist_env": {}, "mcp_env": {"X": "70"}},
     "code_default", "40", False),
    ("process_and_plist_same",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {"X": "60"}, "mcp_env": {}},
     "launchd_plist", "60", False),
    ("process_and_mcp_same",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {}, "mcp_env": {"X": "60"}},
     "mcp_server_env", "60", False),
    ("all_three_same",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {"X": "60"}, "mcp_env": {"X": "60"}},
     "launchd_plist", "60", False),
    ("process_and_plist_differ",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {"X": "80"}, "mcp_env": {}},
     "process_env", "60", False),
    ("three_way_conflict",
     {"default": "40", "environ": {"X": "60"}, "plist_env": {"X": "80"}, "mcp_env": {"X": "90"}},
     "process_env", "60", True),
    ("nowhere_empty_collections",
     {"default": "40", "environ": {}, "plist_env": {}, "mcp_env": {}},
     "code_default", "40", False),
]


def _resolve(kwargs):
    return mod.resolve_env_var("X", **kwargs)


def _layer(result, name):
    matches = [l for l in result["layers"] if l["layer"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} layer, got {result['layers']}"
    return matches[0]


def _winners(result):
    return [l for l in result["layers"] if l["effective_in_process"]]


def test_process_env_only_winner():
    """Value in process_env only: process_env layer wins, code_default does not."""
    result = _resolve(SCENARIOS[0][1])
    assert _layer(result, "process_env")["effective_in_process"] is True
    assert _layer(result, "code_default")["effective_in_process"] is False


def test_plist_value_does_not_reach_process():
    """Value in plist_env but NOT in environ: plist layer False, code_default True."""
    result = _resolve(SCENARIOS[1][1])
    assert _layer(result, "launchd_plist")["effective_in_process"] is False
    assert _layer(result, "code_default")["effective_in_process"] is True
    assert result["effective"] == "40"
    assert result["source"] == "code_default"


def test_mcp_value_does_not_reach_process():
    result = _resolve(SCENARIOS[2][1])
    assert _layer(result, "mcp_server_env")["effective_in_process"] is False
    assert _layer(result, "code_default")["effective_in_process"] is True


def test_same_value_in_process_and_plist_single_winner():
    """Value in BOTH environ and plist_env, same value: exactly one True layer."""
    result = _resolve(SCENARIOS[3][1])
    assert len(_winners(result)) == 1


@pytest.mark.parametrize("scenario_id,kwargs,_,__,___", SCENARIOS)
def test_exactly_one_effective_layer(scenario_id, kwargs, _, __, ___):
    """Across every layer of a resolved var, exactly one has the flag True."""
    result = _resolve(kwargs)
    assert sum(1 for l in result["layers"] if l["effective_in_process"]) == 1, scenario_id


@pytest.mark.parametrize("scenario_id,kwargs,expected_source,_,__", SCENARIOS)
def test_winner_layer_matches_source(scenario_id, kwargs, expected_source, _, __):
    """The unique True layer is the one the existing ``source`` field names."""
    result = _resolve(kwargs)
    winners = _winners(result)
    assert len(winners) == 1, scenario_id
    assert winners[0]["layer"] == expected_source == result["source"], scenario_id


def test_flag_is_bool_and_layer_keys_exact():
    """The flag is a real bool and the layer dict gains exactly the one key."""
    result = _resolve(SCENARIOS[7][1])  # three_way_conflict: all four layers
    assert [l["layer"] for l in result["layers"]] == [
        "process_env", "launchd_plist", "mcp_server_env", "code_default",
    ]
    for layer in result["layers"]:
        assert set(layer.keys()) == LAYER_KEYS
        assert isinstance(layer["effective_in_process"], bool)


@pytest.mark.parametrize("scenario_id,kwargs,expected_source,expected_effective,expected_conflict", SCENARIOS)
def test_effective_source_conflict_unchanged(scenario_id, kwargs, expected_source,
                                             expected_effective, expected_conflict):
    """Adding the flag must not perturb effective, source, conflict, or masking."""
    result = _resolve(kwargs)
    assert result["effective"] == expected_effective, scenario_id
    assert result["source"] == expected_source, scenario_id
    assert result["conflict"] is expected_conflict, scenario_id
    assert result["masked"] is False, scenario_id
    assert set(result.keys()) == TOP_LEVEL_KEYS, scenario_id


def test_env_var_spec_unchanged():
    assert [f.name for f in dataclass_fields(mod.EnvVarSpec)] == ["name", "default"]


class TestMaskedSecretVar:
    """NEGATIVE: masking still masks, and the flags stay correct."""

    NAME = "PIPELINE_TEST_TOKEN_XYZ"  # contains TOKEN -> _is_secret True

    def test_masked_process_env_values_and_flags(self):
        result = mod.resolve_env_var(
            self.NAME, default="fallback",
            environ={self.NAME: "s3cr3t"}, plist_env={}, mcp_env={},
        )
        assert result["masked"] is True
        assert result["effective"] == "***"
        assert all(l["value"] == "***" for l in result["layers"])
        assert _layer(result, "process_env")["effective_in_process"] is True
        assert _layer(result, "code_default")["effective_in_process"] is False
        assert len(_winners(result)) == 1

    def test_masked_plist_only_values_and_flags(self):
        result = mod.resolve_env_var(
            self.NAME, default="fallback",
            environ={}, plist_env={self.NAME: "s3cr3t"}, mcp_env={},
        )
        assert result["masked"] is True
        assert result["effective"] == "fallback"
        assert all(l["value"] == "***" for l in result["layers"])
        assert _layer(result, "launchd_plist")["effective_in_process"] is False
        assert _layer(result, "code_default")["effective_in_process"] is True
        assert len(_winners(result)) == 1


class TestEffectiveEnvConfigFlowThrough:
    """effective_env_config's own shape is unchanged; the flag flows through."""

    def test_every_row_layers_carry_flag(self):
        rows = mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
        assert isinstance(rows, list)
        assert len(rows) == len(mod.ENV_VAR_CATALOG)
        for row in rows:
            assert set(row.keys()) == TOP_LEVEL_KEYS
            assert sum(1 for l in row["layers"] if l["effective_in_process"]) == 1
            for layer in row["layers"]:
                assert isinstance(layer["effective_in_process"], bool)

    def test_specific_row_winner(self):
        rows = mod.effective_env_config(
            environ={"PIPELINE_AUTONOMY": "ungated"}, plist_env={}, mcp_env={},
        )
        row = next(r for r in rows if r["name"] == "PIPELINE_AUTONOMY")
        assert row["source"] == "process_env"
        assert row["effective"] == "ungated"
        assert _layer(row, "process_env")["effective_in_process"] is True
        assert _layer(row, "code_default")["effective_in_process"] is False