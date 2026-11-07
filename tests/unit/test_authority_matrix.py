"""Tests for the layered authority matrix: loader, fold and tier resolution.

The matrix is inert data today: nothing consults it yet. These tests pin the
envelope semantics an org layer declares to a team layer, the fail-closed
defaults (every unknown tier, class or file resolves to ``human``) and the
integrity hashes that make a layer file tamper-evident.
"""

import hashlib
import json
from pathlib import Path

import pytest

from pipeline.authority_matrix import (
    FileLayerSource,
    MatrixError,
    Resolved,
    fold,
    load_layer,
    tier_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = REPO_ROOT / "overlord-authority.json"
TIERS = ["human", "auto+notify", "auto"]
DECISION_CLASSES = [
    "high_risk_merge",
    "override_false_positive_verdict",
    "scope_widen",
    "existing_test_edit",
]
NEVER_IDS = [
    "gate_precondition_bypass",
    "test_deletion",
    "matrix_edit",
    "policy_edit",
    "persona_edit",
    "overlord_rule_file_edit",
]


def _write(path, payload):
    """Write ``payload`` as JSON to ``path`` and return ``path``."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_raw(path, text):
    """Write ``text`` verbatim to ``path`` and return ``path``."""
    path.write_text(text, encoding="utf-8")
    return path


def _org_layer(tmp_path, rule, never=None):
    """Write an org layer holding one rule, plus ``never`` when given."""
    payload = {"version": 1, "layer": "org", "rules": [rule]}
    if never is not None:
        payload["never"] = never
    return _write(tmp_path / "org.json", payload)


def _team_layer(tmp_path, rules, name="team.json"):
    """Write a team layer holding ``rules``."""
    return _write(tmp_path / name, {"version": 1, "layer": "team", "rules": rules})


def _rule(rule_id, rule_type, delegation, value, bounds=None, tighter=None, values=None):
    """Build one org-layer rule definition with only the keys it needs."""
    rule = {
        "id": rule_id,
        "class": "demo",
        "type": rule_type,
        "value": value,
        "delegation": delegation,
    }
    if bounds is not None:
        rule["bounds"] = bounds
    if tighter is not None:
        rule["tighter"] = tighter
    if values is not None:
        rule["values"] = values
    return rule


def _ordered_enum(delegation, value):
    return _rule(
        "demo.rule",
        "ordered_enum",
        delegation,
        value,
        bounds={"loosest": "auto+notify"},
        values=TIERS,
    )


def _numeric(delegation, value):
    return _rule(
        "demo.rule", "numeric", delegation, value, bounds={"min": 0, "max": 10}, tighter="lower"
    )


def _set_must_include(delegation, value):
    return _rule("demo.rule", "set", delegation, value, bounds={"must_include": ["a", "b"]})


def _set_may_choose_from(delegation, value):
    return _rule("demo.rule", "set", delegation, value, bounds={"may_choose_from": ["a", "b", "c"]})


def _flag(delegation, value):
    return _rule("demo.rule", "flag", delegation, value, tighter=True)


def _fold_one(tmp_path, rule, attempted):
    """Fold one team rule against one org rule and return the resolution."""
    org = load_layer(_org_layer(tmp_path, rule))
    team = load_layer(_team_layer(tmp_path, [{"id": rule["id"], "value": attempted}]))
    return fold([org, team])


# (case id, org rule, team value, applied?, violation count)
ENVELOPE_CASES = [
    # ordered_enum: values run tightest -> loosest, so a lower index is tighter.
    (
        "ordered_enum_tighten_only_accept",
        _ordered_enum("tighten_only", "auto+notify"),
        "human",
        True,
        0,
    ),
    (
        "ordered_enum_tighten_only_equal_accept",
        _ordered_enum("tighten_only", "auto+notify"),
        "auto+notify",
        True,
        0,
    ),
    (
        "ordered_enum_tighten_only_reject_looser",
        _ordered_enum("tighten_only", "auto+notify"),
        "auto",
        False,
        1,
    ),
    (
        "ordered_enum_adjustable_accept_at_loosest",
        _ordered_enum("adjustable", "auto+notify"),
        "auto+notify",
        True,
        0,
    ),
    (
        "ordered_enum_adjustable_reject_past_loosest",
        _ordered_enum("adjustable", "auto+notify"),
        "auto",
        False,
        1,
    ),
    (
        "ordered_enum_locked_reject_tighter",
        _ordered_enum("locked", "auto+notify"),
        "human",
        False,
        1,
    ),
    (
        "ordered_enum_locked_reject_equal",
        _ordered_enum("locked", "auto"),
        "auto",
        False,
        1,
    ),
    # numeric: tighter is "lower", so a larger number is looser.
    ("numeric_tighten_only_accept", _numeric("tighten_only", 5), 3, True, 0),
    ("numeric_tighten_only_equal_accept", _numeric("tighten_only", 5), 5, True, 0),
    ("numeric_tighten_only_reject_looser", _numeric("tighten_only", 5), 6, False, 1),
    ("numeric_adjustable_accept_at_max", _numeric("adjustable", 5), 10, True, 0),
    ("numeric_adjustable_accept_at_min", _numeric("adjustable", 5), 0, True, 0),
    ("numeric_adjustable_reject_above_max", _numeric("adjustable", 5), 11, False, 1),
    ("numeric_adjustable_reject_below_min", _numeric("adjustable", 5), -1, False, 1),
    ("numeric_locked_reject_tighter", _numeric("locked", 5), 3, False, 1),
    # set with must_include: tighter means a superset.
    (
        "set_must_include_tighten_only_accept_superset",
        _set_must_include("tighten_only", ["a", "b"]),
        ["a", "b", "c"],
        True,
        0,
    ),
    (
        "set_must_include_tighten_only_equal_accept",
        _set_must_include("tighten_only", ["a", "b"]),
        ["a", "b"],
        True,
        0,
    ),
    (
        "set_must_include_tighten_only_reject_subset",
        _set_must_include("tighten_only", ["a", "b"]),
        ["a"],
        False,
        1,
    ),
    (
        "set_must_include_adjustable_accept_superset",
        _set_must_include("adjustable", ["a", "b"]),
        ["a", "b", "c"],
        True,
        0,
    ),
    (
        "set_must_include_adjustable_reject_outside_bound",
        _set_must_include("adjustable", ["a", "b"]),
        ["c"],
        False,
        1,
    ),
    (
        "set_must_include_locked_reject_superset",
        _set_must_include("locked", ["a", "b"]),
        ["a", "b", "c"],
        False,
        1,
    ),
    # set with may_choose_from: tighter means a subset.
    (
        "set_may_choose_from_tighten_only_accept_subset",
        _set_may_choose_from("tighten_only", ["a", "b"]),
        ["a"],
        True,
        0,
    ),
    (
        "set_may_choose_from_tighten_only_reject_wider",
        _set_may_choose_from("tighten_only", ["a", "b"]),
        ["a", "b", "c", "d"],
        False,
        1,
    ),
    (
        "set_may_choose_from_adjustable_accept_within_bound",
        _set_may_choose_from("adjustable", ["a", "b"]),
        ["a", "b", "c"],
        True,
        0,
    ),
    (
        "set_may_choose_from_adjustable_reject_outside_bound",
        _set_may_choose_from("adjustable", ["a", "b"]),
        ["a", "d"],
        False,
        1,
    ),
    (
        "set_may_choose_from_locked_reject_subset",
        _set_may_choose_from("locked", ["a", "b"]),
        ["a"],
        False,
        1,
    ),
    # flag: tighter is True.
    ("flag_tighten_only_accept_equal", _flag("tighten_only", True), True, True, 0),
    ("flag_tighten_only_reject_looser", _flag("tighten_only", True), False, False, 1),
    ("flag_adjustable_accept_true", _flag("adjustable", False), True, True, 0),
    ("flag_adjustable_accept_false", _flag("adjustable", True), False, True, 0),
    ("flag_locked_reject_equal", _flag("locked", True), True, False, 1),
]


@pytest.mark.parametrize(
    ("rule", "attempted", "applied", "violation_count"),
    [case[1:] for case in ENVELOPE_CASES],
    ids=[case[0] for case in ENVELOPE_CASES],
)
def test_envelope_matrix(tmp_path, rule, attempted, applied, violation_count):
    """Every type/delegation cell accepts inside its envelope and rejects past it."""
    resolved = _fold_one(tmp_path, rule, attempted)

    assert resolved.values[rule["id"]] == (attempted if applied else rule["value"])
    assert len(resolved.violations) == violation_count


@pytest.mark.parametrize(
    "rule",
    [_ordered_enum("tighten_only", "auto+notify"), _numeric("adjustable", 5), _flag("locked", True)],
    ids=["ordered_enum", "numeric", "flag"],
)
def test_rejection_keeps_the_stricter_value_and_records_the_attempt(tmp_path, rule):
    """A rejected team value is recorded, never raised, and never applied."""
    attempted = "auto" if rule["type"] == "ordered_enum" else 99
    if rule["type"] == "flag":
        attempted = True

    resolved = _fold_one(tmp_path, rule, attempted)

    assert resolved.values[rule["id"]] == rule["value"]
    assert resolved.violations == [
        {
            "layer": "team",
            "rule_id": rule["id"],
            "attempted": attempted,
            "reason": resolved.violations[0]["reason"],
        }
    ]
    assert isinstance(resolved.violations[0]["reason"], str)
    assert resolved.violations[0]["reason"]


@pytest.mark.parametrize("delegation", ["adjustable", "tighten_only", "locked"])
def test_never_list_rule_rejects_every_team_override(tmp_path, delegation):
    """A never-list id stays false whatever its delegation and the team ask."""
    rule = _rule("test_deletion", "flag", delegation, False, tighter=False)
    org = load_layer(_org_layer(tmp_path, rule, never=["test_deletion"]))
    team = load_layer(_team_layer(tmp_path, [{"id": "test_deletion", "value": True}]))

    resolved = fold([org, team])

    assert resolved.values["test_deletion"] is False
    assert resolved.violations == [
        {
            "layer": "team",
            "rule_id": "test_deletion",
            "attempted": True,
            "reason": resolved.violations[0]["reason"],
        }
    ]


def test_unknown_rule_id_is_a_violation_and_adds_no_value(tmp_path):
    """A team may only tighten a rule the org layer defines."""
    org = load_layer(_org_layer(tmp_path, _flag("adjustable", False)))
    team = load_layer(_team_layer(tmp_path, [{"id": "ghost.rule", "value": True}]))

    resolved = fold([org, team])

    assert resolved.values == {"demo.rule": False}
    assert resolved.violations == [
        {
            "layer": "team",
            "rule_id": "ghost.rule",
            "attempted": True,
            "reason": resolved.violations[0]["reason"],
        }
    ]


@pytest.mark.parametrize("extra_key", ["delegation", "bounds", "class", "type", "values", "never"])
def test_team_rule_with_extra_keys_is_rejected_by_the_loader(tmp_path, extra_key):
    """A team rule carries only id and value; anything else is a schema error."""
    rule = {"id": "demo.rule", "value": True, extra_key: "anything"}
    team_path = _team_layer(tmp_path, [rule])

    with pytest.raises(MatrixError):
        load_layer(team_path)


@pytest.mark.parametrize("extra_key", ["delegation", "bounds", "class"])
def test_unreadable_team_layer_leaves_the_org_value_in_force(tmp_path, extra_key):
    """An invalid team file is one violation; the org layer alone applies."""
    org_path = _org_layer(tmp_path, _flag("adjustable", False))
    team_path = _team_layer(tmp_path, [{"id": "demo.rule", "value": True, extra_key: "anything"}])

    resolved = FileLayerSource(org_path, team_path).resolve()

    assert resolved is not None
    assert resolved.values == {"demo.rule": False}
    assert resolved.violations == [{"layer": "team", "reason": "unreadable"}]


def test_malformed_json_raises_matrix_error(tmp_path):
    path = _write_raw(tmp_path / "broken.json", '{"version": 1, "layer": "org"')

    with pytest.raises(MatrixError):
        load_layer(path)


def test_missing_file_raises_matrix_error(tmp_path):
    with pytest.raises(MatrixError):
        load_layer(tmp_path / "absent.json")


def _base_org(rule):
    return {"version": 1, "layer": "org", "rules": [rule]}


INVALID_LAYERS = [
    (
        "unknown_type",
        _base_org(
            {"id": "demo.rule", "class": "demo", "type": "mystery", "value": 1, "delegation": "locked"}
        ),
    ),
    (
        "duplicate_values",
        _base_org(_rule("demo.rule", "ordered_enum", "locked", "human",
                        bounds={"loosest": "auto"}, values=["human", "auto", "auto"])),
    ),
    (
        "loosest_not_in_values",
        _base_org(_rule("demo.rule", "ordered_enum", "locked", "human",
                        bounds={"loosest": "ghost"}, values=TIERS)),
    ),
    (
        "ordered_enum_value_outside_values",
        _base_org(_rule("demo.rule", "ordered_enum", "locked", "ghost",
                        bounds={"loosest": "auto"}, values=TIERS)),
    ),
    (
        "numeric_value_outside_bounds",
        _base_org(_rule("demo.rule", "numeric", "adjustable", 42,
                        bounds={"min": 0, "max": 10}, tighter="lower")),
    ),
    (
        "numeric_min_above_max",
        _base_org(_rule("demo.rule", "numeric", "adjustable", 5,
                        bounds={"min": 10, "max": 0}, tighter="lower")),
    ),
    (
        "numeric_unknown_tighter_direction",
        _base_org(_rule("demo.rule", "numeric", "adjustable", 5,
                        bounds={"min": 0, "max": 10}, tighter="sideways")),
    ),
    (
        "unknown_delegation",
        _base_org(_rule("demo.rule", "flag", "sometimes", False, tighter=False)),
    ),
    (
        "missing_value",
        _base_org({"id": "demo.rule", "class": "demo", "type": "flag",
                   "delegation": "locked", "tighter": False}),
    ),
    (
        "missing_bounds",
        _base_org({"id": "demo.rule", "class": "demo", "type": "ordered_enum",
                   "value": "human", "delegation": "locked", "values": TIERS}),
    ),
    (
        "set_bounds_with_both_forms",
        _base_org(_rule("demo.rule", "set", "adjustable", ["a"],
                        bounds={"must_include": ["a"], "may_choose_from": ["a"]})),
    ),
    (
        "set_bounds_with_neither_form",
        _base_org(_rule("demo.rule", "set", "adjustable", ["a"], bounds={})),
    ),
    (
        "flag_tighter_not_boolean",
        _base_org(_rule("demo.rule", "flag", "locked", False, tighter="no")),
    ),
    (
        "version_not_one",
        {"version": 2, "layer": "org",
         "rules": [_rule("demo.rule", "flag", "locked", False, tighter=False)]},
    ),
    (
        "unknown_layer_name",
        {"version": 1, "layer": "tenant",
         "rules": [_rule("demo.rule", "flag", "locked", False, tighter=False)]},
    ),
    (
        "missing_rule_id",
        _base_org({"class": "demo", "type": "flag", "value": False,
                   "delegation": "locked", "tighter": False}),
    ),
    (
        "team_rule_carrying_extra_keys",
        {"version": 1, "layer": "team",
         "rules": [{"id": "demo.rule", "value": True, "bounds": {"loosest": "auto"}}]},
    ),
]


@pytest.mark.parametrize(
    "payload", [case[1] for case in INVALID_LAYERS], ids=[case[0] for case in INVALID_LAYERS]
)
def test_schema_violations_raise_matrix_error(tmp_path, payload):
    """Every malformed layer fails closed at load time."""
    path = _write(tmp_path / "layer.json", payload)

    with pytest.raises(MatrixError):
        load_layer(path)


def test_layer_shas_hash_the_file_bytes_and_track_one_byte(tmp_path):
    """The digest covers raw bytes, so a whitespace edit still changes the sha."""
    org_path = _org_layer(tmp_path, _flag("adjustable", False))
    raw = org_path.read_bytes()
    org = load_layer(org_path)
    team = load_layer(_team_layer(tmp_path, [{"id": "demo.rule", "value": False}]))

    first = fold([team, org])
    org_path.write_bytes(raw + b"\n")
    second = fold([team, load_layer(org_path)])

    assert first.layer_shas[0] == hashlib.sha256(raw).hexdigest()
    assert first.layer_shas[1] == hashlib.sha256(team.raw).hexdigest()
    assert second.layer_shas[0] != first.layer_shas[0]
    assert json.loads(raw) == json.loads(org_path.read_bytes())


def test_rejected_values_never_overwrite_the_running_state(tmp_path):
    """Two rejected team attempts both compare against the still-org value."""
    org = load_layer(_org_layer(tmp_path, _ordered_enum("tighten_only", "human")))
    team_one = load_layer(_team_layer(tmp_path, [{"id": "demo.rule", "value": "auto"}], "t1.json"))
    team_two = load_layer(
        _team_layer(tmp_path, [{"id": "demo.rule", "value": "auto+notify"}], "t2.json")
    )

    resolved = fold([org, team_one, team_two])

    assert resolved.values == {"demo.rule": "human"}
    assert len(resolved.violations) == 2
    assert [violation["attempted"] for violation in resolved.violations] == ["auto", "auto+notify"]
    assert {violation["rule_id"] for violation in resolved.violations} == {"demo.rule"}


def test_tier_for_none_resolves_to_human():
    assert tier_for(None, "high_risk_merge") == "human"


def test_tier_for_unknown_class_resolves_to_human():
    resolved = Resolved(values={"high_risk_merge.tier": "auto"}, violations=[], layer_shas=[])

    assert tier_for(resolved, "never_heard_of_it") == "human"


def test_tier_for_unknown_tier_string_resolves_to_human():
    resolved = Resolved(values={"high_risk_merge.tier": "bogus"}, violations=[], layer_shas=[])

    assert tier_for(resolved, "high_risk_merge") == "human"


def test_tier_for_known_class_returns_the_resolved_tier():
    resolved = Resolved(values={"high_risk_merge.tier": "auto+notify"}, violations=[], layer_shas=[])

    assert tier_for(resolved, "high_risk_merge") == "auto+notify"


def test_file_layer_source_returns_none_for_a_missing_org_layer(tmp_path, caplog):
    with caplog.at_level("ERROR"):
        resolved = FileLayerSource(tmp_path / "absent-authority.json").resolve()

    assert resolved is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"
    assert "absent-authority.json" in caplog.records[0].getMessage()
    assert "MatrixError" in caplog.records[0].getMessage()


def test_file_layer_source_logs_no_file_contents(tmp_path, caplog):
    org_path = _write_raw(tmp_path / "secret-org.json", '{"top-secret-token": ')

    with caplog.at_level("ERROR"):
        resolved = FileLayerSource(org_path).resolve()

    assert resolved is None
    assert len(caplog.records) == 1
    assert "secret-org.json" in caplog.records[0].getMessage()
    assert "top-secret-token" not in caplog.records[0].getMessage()


def test_file_layer_source_reads_both_paths_from_the_environment(tmp_path, monkeypatch):
    org_path = _org_layer(tmp_path, _ordered_enum("adjustable", "human"))
    team_path = _team_layer(tmp_path, [{"id": "demo.rule", "value": "auto+notify"}])
    monkeypatch.setenv("PIPELINE_AUTHORITY_MATRIX", str(org_path))
    monkeypatch.setenv("PIPELINE_AUTHORITY_TEAM_LAYER", str(team_path))

    resolved = FileLayerSource().resolve()

    assert resolved is not None
    assert resolved.values == {"demo.rule": "auto+notify"}
    assert resolved.violations == []


def test_file_layer_source_without_a_team_env_var_uses_the_org_layer_alone(tmp_path, monkeypatch):
    org_path = _org_layer(tmp_path, _ordered_enum("adjustable", "auto+notify"))
    monkeypatch.setenv("PIPELINE_AUTHORITY_MATRIX", str(org_path))
    monkeypatch.delenv("PIPELINE_AUTHORITY_TEAM_LAYER", raising=False)

    resolved = FileLayerSource().resolve()

    assert resolved is not None
    assert resolved.values == {"demo.rule": "auto+notify"}
    assert resolved.violations == []


def test_shipped_matrix_loads_as_an_org_layer():
    assert load_layer(SHIPPED).layer == "org"


@pytest.mark.parametrize("decision_class", DECISION_CLASSES)
def test_shipped_matrix_ships_every_class_at_the_human_tier(decision_class):
    layer = load_layer(SHIPPED)

    assert layer.rules[f"{decision_class}.tier"]["type"] == "ordered_enum"
    assert layer.rules[f"{decision_class}.tier"]["values"] == TIERS
    assert layer.rules[f"{decision_class}.tier"]["value"] == "human"


def test_shipped_matrix_never_list_is_locked_and_false():
    layer = load_layer(SHIPPED)

    assert sorted(layer.never) == sorted(NEVER_IDS)
    assert all(layer.rules[rule_id]["value"] is False for rule_id in layer.never)
    assert all(layer.rules[rule_id]["delegation"] == "locked" for rule_id in layer.never)


def test_shipped_matrix_grants_no_autonomy_through_a_team_layer(tmp_path):
    team_path = _team_layer(
        tmp_path,
        [{"id": f"{decision_class}.tier", "value": "auto"} for decision_class in DECISION_CLASSES],
    )

    resolved = FileLayerSource(SHIPPED, team_path).resolve()

    assert resolved is not None
    assert {resolved.values[f"{decision_class}.tier"] for decision_class in DECISION_CLASSES} == {
        "human"
    }
    assert len(resolved.violations) == len(DECISION_CLASSES)
