from pipeline import file_size_policy as file_size_policy_module
from pipeline.file_size_policy import (
    FILE_SIZE_POLICY_ADVISORY,
    FILE_SIZE_POLICY_ENFORCED,
    FILE_SIZE_POLICY_VALUES,
    resolve_file_size_policy,
)
from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR


def _policy_settings(org=None, team=None, allow=None):
    settings = {}
    org_layer = {}
    if org is not None:
        org_layer["file_size_policy"] = org
    if allow is not None:
        org_layer["allow_team_loosen"] = allow
    if org_layer:
        settings["org"] = org_layer
    if team is not None:
        settings["team"] = {"file_size_policy": team}
    return settings


def _rejection(layer, by):
    return {
        "layer": layer,
        "requested": FILE_SIZE_POLICY_ADVISORY,
        "by": by,
    }


def _resolve_triple(result):
    return (result["file_size_policy"], result["source"], result["lock_rejections"])


def test_constants_match_the_public_values():
    assert FILE_SIZE_POLICY_ADVISORY == "advisory"
    assert FILE_SIZE_POLICY_ENFORCED == "enforced"
    assert FILE_SIZE_POLICY_VALUES == ("advisory", "enforced")


def test_defaults_to_advisory_when_nothing_is_set():
    result = resolve_file_size_policy(settings=_policy_settings())

    assert _resolve_triple(result) == ("advisory", "default", [])


def test_org_enforced_policy_is_applied():
    result = resolve_file_size_policy(settings=_policy_settings(org="enforced"))

    assert _resolve_triple(result) == ("enforced", "org", [])


def test_org_advisory_policy_is_applied():
    result = resolve_file_size_policy(settings=_policy_settings(org="advisory"))

    assert _resolve_triple(result) == ("advisory", "org", [])


def test_team_enforced_tightens_over_org_advisory():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="advisory", team="enforced")
    )

    assert _resolve_triple(result) == ("enforced", "team", [])


def test_team_advisory_is_refused_by_org_enforced_without_lock():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced", team="advisory")
    )

    assert _resolve_triple(result) == (
        "enforced",
        "org",
        [_rejection("team", "org")],
    )


def test_team_advisory_is_allowed_when_org_sets_lock_true():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced", team="advisory", allow=True)
    )

    assert _resolve_triple(result) == ("advisory", "team", [])


def test_truthy_non_bool_lock_does_not_unlock_team_loosening():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced", team="advisory", allow="yes")
    )

    assert _resolve_triple(result) == (
        "enforced",
        "org",
        [_rejection("team", "org")],
    )


def test_env_enforced_tightens_over_advisory_policy():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="advisory"), env_value="enforced"
    )

    assert _resolve_triple(result) == ("enforced", "env", [])


def test_env_advisory_is_refused_by_org_enforced():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced"), env_value="advisory"
    )

    assert _resolve_triple(result) == (
        "enforced",
        "org",
        [_rejection("env", "org")],
    )


def test_env_advisory_is_applied_when_no_policy_is_set():
    result = resolve_file_size_policy(settings=_policy_settings(), env_value="advisory")

    assert _resolve_triple(result) == ("advisory", "env", [])


def test_unknown_env_value_is_treated_as_advisory_and_recorded_invalid():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced"), env_value="strict"
    )

    assert _resolve_triple(result) == (
        "enforced",
        "org",
        [_rejection("env", "org")],
    )
    assert result["invalid_layers"] == ["env"]


def test_blank_env_value_is_ignored():
    result = resolve_file_size_policy(
        settings=_policy_settings(org="enforced"), env_value="   "
    )

    assert _resolve_triple(result) == ("enforced", "org", [])
    assert result["invalid_layers"] == []


def test_provider_error_fails_closed_to_enforced():
    result = resolve_file_size_policy(
        settings={SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR}
    )

    assert _resolve_triple(result) == (
        "enforced",
        SETTINGS_PROVIDER_ERROR,
        [],
    )


def test_provider_error_records_refused_env_advisory_request():
    result = resolve_file_size_policy(
        settings={SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR},
        env_value="advisory",
    )

    assert _resolve_triple(result) == (
        "enforced",
        SETTINGS_PROVIDER_ERROR,
        [
            {
                "layer": "env",
                "requested": FILE_SIZE_POLICY_ADVISORY,
                "by": SETTINGS_PROVIDER_ERROR,
            }
        ],
    )


def test_unknown_org_value_falls_back_to_advisory_and_is_recorded_invalid():
    result = resolve_file_size_policy(settings=_policy_settings(org="enforce"))

    assert _resolve_triple(result) == ("advisory", "org", [])
    assert result["invalid_layers"] == ["org"]


def test_non_string_team_value_falls_back_to_advisory_and_is_recorded_invalid():
    result = resolve_file_size_policy(settings=_policy_settings(team=3))

    assert _resolve_triple(result) == ("advisory", "team", [])
    assert result["invalid_layers"] == ["team"]


def test_none_settings_resolve_to_default():
    result = resolve_file_size_policy(settings=None)

    assert _resolve_triple(result) == ("advisory", "default", [])


def test_non_dict_settings_resolve_to_default():
    result = resolve_file_size_policy(settings="enforced")

    assert _resolve_triple(result) == ("advisory", "default", [])


def test_settings_without_org_or_team_resolve_to_default():
    result = resolve_file_size_policy(
        settings={"other": {"file_size_policy": "enforced"}}
    )

    assert _resolve_triple(result) == ("advisory", "default", [])


def test_result_always_carries_all_four_keys():
    result = resolve_file_size_policy(settings=None)

    assert set(result) == {
        "file_size_policy",
        "source",
        "lock_rejections",
        "invalid_layers",
    }


def test_resolver_module_does_not_import_os():
    assert "os" not in vars(file_size_policy_module)
