import dataclasses

import pytest

from pipeline.settings_registry import REGISTRY, SettingKey
from pipeline.test_scope import TEST_SCOPE_FULL, TEST_SCOPE_SCOPED


def test_registry_holds_exactly_the_eight_plan_keys():
    assert set(REGISTRY) == {
        "merge_ci_gate",
        "merge_build_gate",
        "test_scope",
        "merge_ci_timeout_s",
        "merge_readiness_timeout_s",
        "rework_on_ci_fail",
        "reverify_full_suite",
        "grade_xdist_workers",
        "role_routing",
    }


def test_registry_entries_are_named_after_their_keys():
    assert all(name == key.name for name, key in REGISTRY.items())


def test_merge_ci_gate_is_a_bool_ceiling_with_safe_value_on():
    key = REGISTRY["merge_ci_gate"]
    assert (key.kind, key.type, key.safe_value) == ("ceiling", "bool", True)


def test_merge_build_gate_is_a_bool_ceiling_with_safe_value_on():
    key = REGISTRY["merge_build_gate"]
    assert (key.kind, key.type, key.safe_value) == ("ceiling", "bool", True)


def test_test_scope_is_an_enum_ceiling_with_safe_value_full():
    key = REGISTRY["test_scope"]
    assert (key.kind, key.type, key.safe_value) == ("ceiling", "enum", TEST_SCOPE_FULL)


def test_merge_ci_timeout_is_an_int_pref_with_safe_value_900():
    key = REGISTRY["merge_ci_timeout_s"]
    assert (key.kind, key.type, key.safe_value) == ("pref", "int", 900)


def test_merge_readiness_timeout_is_an_int_pref_with_safe_value_180():
    key = REGISTRY["merge_readiness_timeout_s"]
    assert (key.kind, key.type, key.safe_value) == ("pref", "int", 180)


def test_rework_on_ci_fail_is_a_bool_pref_with_safe_value_off():
    key = REGISTRY["rework_on_ci_fail"]
    assert (key.kind, key.type, key.safe_value) == ("pref", "bool", False)


def test_reverify_full_suite_is_a_bool_pref_with_safe_value_off():
    key = REGISTRY["reverify_full_suite"]
    assert (key.kind, key.type, key.safe_value) == ("pref", "bool", False)


def test_grade_xdist_workers_is_an_int_ceiling_with_safe_value_one():
    key = REGISTRY["grade_xdist_workers"]
    assert (key.kind, key.type, key.safe_value) == ("ceiling", "int", 1)


def test_legacy_env_names_are_the_plan_names():
    assert {name: key.legacy_env for name, key in REGISTRY.items()} == {
        "merge_ci_gate": "PIPELINE_MERGE_CI_GATE",
        "merge_build_gate": "PIPELINE_MERGE_BUILD_GATE",
        "test_scope": "PIPELINE_TEST_SCOPE",
        "merge_ci_timeout_s": "PIPELINE_MERGE_CI_TIMEOUT",
        "merge_readiness_timeout_s": "PIPELINE_MERGE_READINESS_TIMEOUT",
        "rework_on_ci_fail": "PIPELINE_REWORK_ON_CI_FAIL",
        "reverify_full_suite": "PIPELINE_REVERIFY_FULL_SUITE",
        "grade_xdist_workers": "PIPELINE_GRADE_XDIST_WORKERS",
        "role_routing": "",
    }


def test_test_scope_full_is_tighter_than_scoped():
    tighter = REGISTRY["test_scope"].tighter
    assert tighter(TEST_SCOPE_SCOPED, TEST_SCOPE_FULL) == TEST_SCOPE_FULL


def test_test_scope_full_is_tighter_than_scoped_in_either_order():
    tighter = REGISTRY["test_scope"].tighter
    assert tighter(TEST_SCOPE_FULL, TEST_SCOPE_SCOPED) == TEST_SCOPE_FULL


def test_test_scope_allows_only_full_and_scoped():
    assert REGISTRY["test_scope"].choices == (TEST_SCOPE_FULL, TEST_SCOPE_SCOPED)


def test_on_is_tighter_than_off_for_merge_ci_gate():
    tighter = REGISTRY["merge_ci_gate"].tighter
    assert tighter(False, True) is True


def test_on_is_tighter_than_off_for_merge_build_gate():
    tighter = REGISTRY["merge_build_gate"].tighter
    assert tighter(True, False) is True


def test_lower_worker_count_is_tighter_for_grade_xdist_workers():
    tighter = REGISTRY["grade_xdist_workers"].tighter
    assert tighter(1, 4) == 1


def test_higher_worker_count_is_not_tighter_for_grade_xdist_workers():
    tighter = REGISTRY["grade_xdist_workers"].tighter
    assert tighter(4, 1) == 1


def test_pref_keys_declare_no_tighter_function():
    assert REGISTRY["merge_ci_timeout_s"].tighter is None


def test_setting_key_is_immutable():
    key = REGISTRY["merge_ci_gate"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.safe_value = False


def test_setting_key_is_a_setting_key_instance():
    assert isinstance(REGISTRY["merge_ci_gate"], SettingKey)
