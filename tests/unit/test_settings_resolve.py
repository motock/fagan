import pytest

from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR
from pipeline.settings_registry import REGISTRY, SettingKey
from pipeline.settings_resolve import resolve
from pipeline.test_scope import TEST_SCOPE_FULL, TEST_SCOPE_SCOPED

TIMEOUT = "merge_ci_timeout_s"
TEST_SCOPE = "test_scope"
CI_GATE = "merge_ci_gate"
WORKERS = "grade_xdist_workers"
REWORK = "rework_on_ci_fail"
LOCK_KEY = "lock_probe"


def _lock_key():
    return SettingKey(
        name=LOCK_KEY,
        type="int",
        kind="lock",
        safe_value=900,
        allowed_layers=("deployment",),
        legacy_env="PIPELINE_LOCK_PROBE",
    )


@pytest.fixture
def lock_registry(monkeypatch):
    monkeypatch.setitem(REGISTRY, LOCK_KEY, _lock_key())


# --- pref: most specific valid layer wins ---


def test_pref_most_specific_layer_wins():
    result = resolve(
        TIMEOUT, {"deployment": {TIMEOUT: 600}, "team_repo": {TIMEOUT: 300}}
    )
    assert (result["value"], result["source"]) == (300, "team_repo")


def test_pref_falls_back_to_less_specific_layer_when_specific_is_absent():
    result = resolve(TIMEOUT, {"deployment": {TIMEOUT: 600}, "team": {}})
    assert (result["value"], result["source"]) == (600, "deployment")


def test_pref_bool_team_value_is_applied():
    result = resolve(REWORK, {"team": {REWORK: True}})
    assert (result["value"], result["source"]) == (True, "team")


def test_pref_request_is_ignored_when_key_does_not_allow_request():
    result = resolve(TIMEOUT, {}, request=300)
    assert (result["value"], result["source"]) == (900, "safe_default")
    assert result["invalid_layers"] == ["request"]


# --- ceiling: tighten-only unless the providing layer opts in ---


def test_ceiling_tighter_value_at_lower_layer_is_applied():
    result = resolve(CI_GATE, {"deployment": {CI_GATE: False}, "team": {CI_GATE: True}})
    assert (result["value"], result["source"]) == (True, "team")


def test_ceiling_equal_value_at_lower_layer_is_accepted_and_source_moves_down():
    result = resolve(
        TEST_SCOPE,
        {"deployment": {TEST_SCOPE: "scoped"}, "team": {TEST_SCOPE: "scoped"}},
    )
    assert (result["value"], result["source"]) == (TEST_SCOPE_SCOPED, "team")
    assert result["lock_rejections"] == []


def test_ceiling_loosen_is_refused_without_allow_loosen():
    result = resolve(
        TEST_SCOPE, {"deployment": {TEST_SCOPE: "full"}, "team": {TEST_SCOPE: "scoped"}}
    )
    assert (result["value"], result["source"]) == (TEST_SCOPE_FULL, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": TEST_SCOPE_SCOPED, "by": "deployment"}
    ]


def test_ceiling_loosen_is_accepted_when_providing_layer_allows_it():
    layers = {
        "deployment": {TEST_SCOPE: "full", "allow_loosen": [TEST_SCOPE]},
        "team": {TEST_SCOPE: "scoped"},
    }
    result = resolve(TEST_SCOPE, layers)
    assert (result["value"], result["source"]) == (TEST_SCOPE_SCOPED, "team")
    assert result["lock_rejections"] == []


def test_ceiling_loosen_is_refused_when_allow_loosen_names_another_key():
    layers = {
        "deployment": {TEST_SCOPE: "full", "allow_loosen": [CI_GATE]},
        "team": {TEST_SCOPE: "scoped"},
    }
    result = resolve(TEST_SCOPE, layers)
    assert result["value"] == TEST_SCOPE_FULL
    assert len(result["lock_rejections"]) == 1


def test_ceiling_loosen_is_refused_when_allow_loosen_is_a_bare_string():
    layers = {
        "deployment": {TEST_SCOPE: "full", "allow_loosen": TEST_SCOPE},
        "team": {TEST_SCOPE: "scoped"},
    }
    result = resolve(TEST_SCOPE, layers)
    assert result["value"] == TEST_SCOPE_FULL
    assert len(result["lock_rejections"]) == 1


def test_ceiling_bool_loosen_from_on_to_off_is_refused():
    result = resolve(CI_GATE, {"deployment": {CI_GATE: True}, "team": {CI_GATE: False}})
    assert (result["value"], result["source"]) == (True, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": False, "by": "deployment"}
    ]


def test_ceiling_int_lower_value_at_lower_layer_is_applied():
    result = resolve(WORKERS, {"deployment": {WORKERS: 4}, "team": {WORKERS: 2}})
    assert (result["value"], result["source"]) == (2, "team")


def test_ceiling_int_higher_value_at_lower_layer_is_refused():
    result = resolve(WORKERS, {"deployment": {WORKERS: 2}, "team": {WORKERS: 4}})
    assert (result["value"], result["source"]) == (2, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": 4, "by": "deployment"}
    ]


def test_ceiling_request_tighter_than_policy_is_applied():
    result = resolve(
        TEST_SCOPE, {"team": {TEST_SCOPE: "scoped"}}, request=TEST_SCOPE_FULL
    )
    assert (result["value"], result["source"]) == (TEST_SCOPE_FULL, "request")


def test_ceiling_request_looser_than_policy_is_refused():
    result = resolve(
        TEST_SCOPE, {"deployment": {TEST_SCOPE: "full"}}, request=TEST_SCOPE_SCOPED
    )
    assert (result["value"], result["source"]) == (TEST_SCOPE_FULL, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "request", "requested": TEST_SCOPE_SCOPED, "by": "deployment"}
    ]


def test_ceiling_request_applies_when_no_policy_is_set():
    result = resolve(TEST_SCOPE, {}, request=TEST_SCOPE_SCOPED)
    assert (result["value"], result["source"]) == (TEST_SCOPE_SCOPED, "request")


# --- lock: only the deployment layer is read ---


def test_lock_reads_deployment_layer(lock_registry):
    result = resolve(LOCK_KEY, {"deployment": {LOCK_KEY: 600}})
    assert (result["value"], result["source"]) == (600, "deployment")


def test_lock_value_set_at_lower_layer_is_ignored_and_reported(lock_registry):
    result = resolve(LOCK_KEY, {"deployment": {LOCK_KEY: 600}, "team": {LOCK_KEY: 300}})
    assert result["value"] == 600
    assert result["invalid_layers"] == ["team"]


def test_lock_without_deployment_value_returns_safe_default(lock_registry):
    result = resolve(LOCK_KEY, {"team": {LOCK_KEY: 300}})
    assert (result["value"], result["source"]) == (900, "safe_default")
    assert result["invalid_layers"] == ["team"]


# --- invalid layer values: recorded and ignored ---


def test_wrong_type_layer_value_is_ignored_and_recorded():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: "300"}})
    assert (result["value"], result["source"]) == (900, "safe_default")
    assert result["invalid_layers"] == ["team"]


def test_wrong_type_does_not_mask_a_valid_less_specific_layer():
    result = resolve(TIMEOUT, {"deployment": {TIMEOUT: 600}, "team": {TIMEOUT: "300"}})
    assert (result["value"], result["source"]) == (600, "deployment")
    assert result["invalid_layers"] == ["team"]


def test_bool_is_rejected_for_int_key():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: True}})
    assert result["invalid_layers"] == ["team"]


def test_int_is_rejected_for_bool_key():
    result = resolve(REWORK, {"team": {REWORK: 1}})
    assert result["invalid_layers"] == ["team"]


def test_zero_timeout_is_rejected_as_invalid():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: 0}})
    assert (result["value"], result["invalid_layers"]) == (900, ["team"])


def test_one_second_timeout_is_accepted_at_the_lower_boundary():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: 1}})
    assert (result["value"], result["source"]) == (1, "team")


def test_negative_timeout_is_rejected_as_invalid():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: -1}})
    assert (result["value"], result["invalid_layers"]) == (900, ["team"])


def test_enum_value_outside_choices_is_rejected_as_invalid():
    result = resolve(TEST_SCOPE, {"team": {TEST_SCOPE: "partial"}})
    assert (result["value"], result["invalid_layers"]) == (TEST_SCOPE_FULL, ["team"])


def test_none_layer_value_is_treated_as_unset_not_invalid():
    result = resolve(TIMEOUT, {"team": {TIMEOUT: None}})
    assert (result["value"], result["source"], result["invalid_layers"]) == (
        900,
        "safe_default",
        [],
    )


# --- empty input and error marker: safe value ---


def test_empty_layers_return_safe_default():
    result = resolve(TIMEOUT, {})
    assert (result["value"], result["source"]) == (900, "safe_default")


def test_error_marker_returns_safe_value_and_ignores_layers():
    layers = {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR, "deployment": {TIMEOUT: 300}}
    result = resolve(TIMEOUT, layers)
    assert (result["value"], result["source"]) == (900, "settings_error")


def test_error_marker_records_refused_scoped_request():
    layers = {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR}
    result = resolve(TEST_SCOPE, layers, request=TEST_SCOPE_SCOPED)
    assert result["value"] == TEST_SCOPE_FULL
    assert result["source"] == "settings_error"
    assert result["lock_rejections"] == [
        {"layer": "request", "requested": TEST_SCOPE_SCOPED, "by": "settings_error"}
    ]


def test_non_mapping_layers_return_safe_value_as_settings_error():
    result = resolve(TIMEOUT, None)
    assert (result["value"], result["source"]) == (900, "settings_error")


# --- unknown key ---


def test_unknown_key_raises_key_error_naming_the_key():
    with pytest.raises(KeyError) as excinfo:
        resolve("no_such_setting", {})
    assert excinfo.value.args == ("no_such_setting",)
