import copy

import pytest

from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR
from pipeline.test_scope import (
    TEST_SCOPE_FULL,
    TEST_SCOPE_SCOPED,
    TEST_SCOPE_VALUES,
    resolve_test_scope,
)

ALLOW_MISSING = "missing"


def _policy_settings(org, team, allow):
    settings = {}
    org_layer = {}
    if org is not None:
        org_layer["test_scope"] = org
    if allow != ALLOW_MISSING:
        org_layer["allow_team_loosen"] = allow
    if org_layer:
        settings["org"] = org_layer
    if team is not None:
        settings["team"] = {"test_scope": team}
    return settings


def _rejection(layer, by):
    return {"layer": layer, "requested": TEST_SCOPE_SCOPED, "by": by}


def _resolve_triple(result):
    return (result["test_scope"], result["source"], result["lock_rejections"])


def test_constants_match_the_public_values():
    assert TEST_SCOPE_VALUES == ("full", "scoped")


# Lock matrix: org in {absent, full, scoped} x team in {absent, full, scoped}
# x allow_team_loosen in {missing, False, True}. Rejection only when org is
# "full", team is "scoped" and the lock is not exactly True.
@pytest.mark.parametrize(
    "org, team, allow, scope, source, rejections",
    [
        pytest.param(
            None,
            None,
            ALLOW_MISSING,
            "full",
            "default",
            [],
            id="org-absent-team-absent-allow-missing",
        ),
        pytest.param(
            None,
            None,
            False,
            "full",
            "default",
            [],
            id="org-absent-team-absent-allow-false",
        ),
        pytest.param(
            None,
            None,
            True,
            "full",
            "default",
            [],
            id="org-absent-team-absent-allow-true",
        ),
        pytest.param(
            None,
            "full",
            ALLOW_MISSING,
            "full",
            "team",
            [],
            id="org-absent-team-full-allow-missing",
        ),
        pytest.param(
            None,
            "full",
            False,
            "full",
            "team",
            [],
            id="org-absent-team-full-allow-false",
        ),
        pytest.param(
            None, "full", True, "full", "team", [], id="org-absent-team-full-allow-true"
        ),
        pytest.param(
            None,
            "scoped",
            ALLOW_MISSING,
            "scoped",
            "team",
            [],
            id="org-absent-team-scoped-allow-missing",
        ),
        pytest.param(
            None,
            "scoped",
            False,
            "scoped",
            "team",
            [],
            id="org-absent-team-scoped-allow-false",
        ),
        pytest.param(
            None,
            "scoped",
            True,
            "scoped",
            "team",
            [],
            id="org-absent-team-scoped-allow-true",
        ),
        pytest.param(
            "full",
            None,
            ALLOW_MISSING,
            "full",
            "org",
            [],
            id="org-full-team-absent-allow-missing",
        ),
        pytest.param(
            "full",
            None,
            False,
            "full",
            "org",
            [],
            id="org-full-team-absent-allow-false",
        ),
        pytest.param(
            "full", None, True, "full", "org", [], id="org-full-team-absent-allow-true"
        ),
        pytest.param(
            "full",
            "full",
            ALLOW_MISSING,
            "full",
            "team",
            [],
            id="org-full-team-full-allow-missing",
        ),
        pytest.param(
            "full",
            "full",
            False,
            "full",
            "team",
            [],
            id="org-full-team-full-allow-false",
        ),
        pytest.param(
            "full", "full", True, "full", "team", [], id="org-full-team-full-allow-true"
        ),
        pytest.param(
            "full",
            "scoped",
            ALLOW_MISSING,
            "full",
            "org",
            [_rejection("team", "org")],
            id="org-full-team-scoped-allow-missing",
        ),
        pytest.param(
            "full",
            "scoped",
            False,
            "full",
            "org",
            [_rejection("team", "org")],
            id="org-full-team-scoped-allow-false",
        ),
        pytest.param(
            "full",
            "scoped",
            True,
            "scoped",
            "team",
            [],
            id="org-full-team-scoped-allow-true",
        ),
        pytest.param(
            "scoped",
            None,
            ALLOW_MISSING,
            "scoped",
            "org",
            [],
            id="org-scoped-team-absent-allow-missing",
        ),
        pytest.param(
            "scoped",
            None,
            False,
            "scoped",
            "org",
            [],
            id="org-scoped-team-absent-allow-false",
        ),
        pytest.param(
            "scoped",
            None,
            True,
            "scoped",
            "org",
            [],
            id="org-scoped-team-absent-allow-true",
        ),
        pytest.param(
            "scoped",
            "full",
            ALLOW_MISSING,
            "full",
            "team",
            [],
            id="org-scoped-team-full-allow-missing",
        ),
        pytest.param(
            "scoped",
            "full",
            False,
            "full",
            "team",
            [],
            id="org-scoped-team-full-allow-false",
        ),
        pytest.param(
            "scoped",
            "full",
            True,
            "full",
            "team",
            [],
            id="org-scoped-team-full-allow-true",
        ),
        pytest.param(
            "scoped",
            "scoped",
            ALLOW_MISSING,
            "scoped",
            "team",
            [],
            id="org-scoped-team-scoped-allow-missing",
        ),
        pytest.param(
            "scoped",
            "scoped",
            False,
            "scoped",
            "team",
            [],
            id="org-scoped-team-scoped-allow-false",
        ),
        pytest.param(
            "scoped",
            "scoped",
            True,
            "scoped",
            "team",
            [],
            id="org-scoped-team-scoped-allow-true",
        ),
    ],
)
def test_lock_matrix_resolves_policy_layer(org, team, allow, scope, source, rejections):
    result = resolve_test_scope(settings=_policy_settings(org, team, allow))

    assert _resolve_triple(result) == (scope, source, rejections)


@pytest.mark.parametrize(
    "allow",
    [
        pytest.param("true", id="allow-string-true"),
        pytest.param(1, id="allow-int-one"),
        pytest.param("yes", id="allow-string-yes"),
    ],
)
def test_non_bool_truthy_allow_is_locked(allow):
    result = resolve_test_scope(settings=_policy_settings("full", "scoped", allow))

    assert _resolve_triple(result) == (
        TEST_SCOPE_FULL,
        "org",
        [_rejection("team", "org")],
    )


@pytest.mark.parametrize(
    "plan_value, story_value, env_value, invalid_layers",
    [
        pytest.param("partial", None, None, ["plan"], id="plan-unknown-string"),
        pytest.param("FULL", None, None, ["plan"], id="plan-uppercase-full-is-invalid"),
        pytest.param(1, None, None, ["plan"], id="plan-int"),
        pytest.param(True, None, None, ["plan"], id="plan-bool-true"),
        pytest.param([], None, None, ["plan"], id="plan-empty-list"),
        pytest.param(None, "partial", None, ["story"], id="story-unknown-string"),
        pytest.param(
            None, "FULL", None, ["story"], id="story-uppercase-full-is-invalid"
        ),
        pytest.param(None, 1, None, ["story"], id="story-int"),
        pytest.param(None, True, None, ["story"], id="story-bool-true"),
        pytest.param(None, [], None, ["story"], id="story-empty-list"),
        pytest.param(None, None, "partial", ["env"], id="env-unknown-string"),
        pytest.param(None, None, "FULL", ["env"], id="env-uppercase-full-is-invalid"),
        pytest.param(None, None, 1, ["env"], id="env-int"),
        pytest.param(None, None, True, ["env"], id="env-bool-true"),
        pytest.param(None, None, [], ["env"], id="env-empty-list"),
    ],
)
def test_invalid_request_value_fails_closed_to_full(
    plan_value, story_value, env_value, invalid_layers
):
    result = resolve_test_scope(
        settings=None,
        plan_value=plan_value,
        story_value=story_value,
        env_value=env_value,
    )

    assert (result["test_scope"], result["invalid_layers"]) == (
        TEST_SCOPE_FULL,
        invalid_layers,
    )


@pytest.mark.parametrize(
    "org_value, expected",
    [
        pytest.param("partial", ("full", "org", ["org"]), id="org-unknown-string"),
        pytest.param(
            "FULL", ("full", "org", ["org"]), id="org-uppercase-full-is-invalid"
        ),
        pytest.param(1, ("full", "org", ["org"]), id="org-int"),
    ],
)
def test_invalid_org_value_counts_as_explicit_full(org_value, expected):
    result = resolve_test_scope(
        settings={"org": {"test_scope": org_value}, "team": {"test_scope": "scoped"}},
        plan_value="scoped",
    )

    assert (
        result["test_scope"],
        result["source"],
        result["invalid_layers"],
    ) == expected


@pytest.mark.parametrize(
    "team_value, expected",
    [
        pytest.param("partial", ("full", "team", ["team"]), id="team-unknown-string"),
        pytest.param(True, ("full", "team", ["team"]), id="team-bool-true"),
        pytest.param([], ("full", "team", ["team"]), id="team-empty-list"),
    ],
)
def test_invalid_team_value_resolves_team_to_full(team_value, expected):
    result = resolve_test_scope(settings={"team": {"test_scope": team_value}})

    assert (
        result["test_scope"],
        result["source"],
        result["invalid_layers"],
    ) == expected


@pytest.mark.parametrize(
    "plan_value",
    [
        pytest.param("", id="plan-empty-string"),
        pytest.param("   ", id="plan-spaces"),
        pytest.param("\t\n", id="plan-tab-and-newline"),
    ],
)
def test_blank_request_value_is_absent_and_falls_through(plan_value):
    result = resolve_test_scope(
        settings=None, plan_value=plan_value, story_value="scoped"
    )

    assert (_resolve_triple(result), result["invalid_layers"]) == (
        (TEST_SCOPE_SCOPED, "story", []),
        [],
    )


def test_padded_valid_request_value_is_accepted():
    result = resolve_test_scope(settings=None, plan_value=" scoped ")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "plan", [])


def test_blank_policy_value_is_absent_and_org_is_silent():
    result = resolve_test_scope(
        settings={"org": {"test_scope": "  "}, "team": {"test_scope": "scoped"}},
    )

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "team", [])


def test_no_layers_resolves_to_default_full():
    result = resolve_test_scope(settings=None)

    assert result == {
        "test_scope": "full",
        "source": "default",
        "lock_rejections": [],
        "invalid_layers": [],
    }


def test_result_has_exactly_the_documented_keys():
    result = resolve_test_scope(settings=None, plan_value="scoped")

    assert set(result) == {"test_scope", "source", "lock_rejections", "invalid_layers"}


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(None, id="settings-none"),
        pytest.param("x", id="settings-string"),
        pytest.param({"org": "full"}, id="org-not-a-dict"),
    ],
)
def test_non_dict_settings_are_treated_as_absent(settings):
    result = resolve_test_scope(settings=settings, plan_value="scoped")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "plan", [])


def test_plan_scoped_beats_story_full():
    result = resolve_test_scope(settings=None, plan_value="scoped", story_value="full")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "plan", [])


def test_story_scoped_beats_env_full():
    result = resolve_test_scope(settings=None, story_value="scoped", env_value="full")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "story", [])


def test_env_scoped_alone_resolves_to_env():
    result = resolve_test_scope(settings=None, env_value="scoped")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "env", [])


def test_plan_scoped_under_org_full_is_rejected_by_org():
    result = resolve_test_scope(
        settings={"org": {"test_scope": "full"}}, plan_value="scoped"
    )

    assert _resolve_triple(result) == (
        TEST_SCOPE_FULL,
        "org",
        [_rejection("plan", "org")],
    )


def test_story_scoped_under_team_full_is_rejected_by_team():
    result = resolve_test_scope(
        settings={"team": {"test_scope": "full"}}, story_value="scoped"
    )

    assert _resolve_triple(result) == (
        TEST_SCOPE_FULL,
        "team",
        [_rejection("story", "team")],
    )


def test_plan_full_under_org_scoped_tightens_without_rejection():
    result = resolve_test_scope(
        settings={"org": {"test_scope": "scoped"}}, plan_value="full"
    )

    assert _resolve_triple(result) == (TEST_SCOPE_FULL, "plan", [])


def test_org_scoped_without_request_resolves_to_org():
    result = resolve_test_scope(settings={"org": {"test_scope": "scoped"}})

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "org", [])


def test_team_scoped_with_lock_under_org_full_lets_plan_scoped_through():
    settings = {
        "org": {"test_scope": "full", "allow_team_loosen": True},
        "team": {"test_scope": "scoped"},
    }
    result = resolve_test_scope(settings=settings, plan_value="scoped")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "team", [])


def test_team_and_plan_rejections_are_both_recorded_in_order():
    settings = {"org": {"test_scope": "full"}, "team": {"test_scope": "scoped"}}
    result = resolve_test_scope(settings=settings, plan_value="scoped")

    assert _resolve_triple(result) == (
        TEST_SCOPE_FULL,
        "org",
        [_rejection("team", "org"), _rejection("plan", "org")],
    )


def test_settings_are_not_mutated():
    settings = {
        "org": {"test_scope": "full", "allow_team_loosen": False},
        "team": {"test_scope": "scoped"},
    }
    before = copy.deepcopy(settings)

    resolve_test_scope(settings=settings, plan_value="scoped", story_value="full")

    assert settings == before


@pytest.mark.parametrize(
    "plan_value, story_value, env_value, expected",
    [
        pytest.param(
            None,
            None,
            None,
            (TEST_SCOPE_FULL, "settings_provider_error", []),
            id="no-requests",
        ),
        pytest.param(
            "scoped",
            None,
            None,
            (
                TEST_SCOPE_FULL,
                "settings_provider_error",
                [_rejection("plan", SETTINGS_PROVIDER_ERROR)],
            ),
            id="plan-scoped-rejected",
        ),
        pytest.param(
            None,
            None,
            "scoped",
            (
                TEST_SCOPE_FULL,
                "settings_provider_error",
                [_rejection("env", SETTINGS_PROVIDER_ERROR)],
            ),
            id="env-scoped-rejected",
        ),
        pytest.param(
            None,
            "full",
            None,
            (TEST_SCOPE_FULL, "settings_provider_error", []),
            id="story-full-not-rejected",
        ),
    ],
)
def test_provider_error_fails_closed(plan_value, story_value, env_value, expected):
    result = resolve_test_scope(
        settings={SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR},
        plan_value=plan_value,
        story_value=story_value,
        env_value=env_value,
    )

    assert _resolve_triple(result) == expected


def test_provider_error_marker_wins_over_org_scoped_policy():
    settings = {
        SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR,
        "org": {"test_scope": "scoped"},
    }
    result = resolve_test_scope(settings=settings)

    assert _resolve_triple(result) == (TEST_SCOPE_FULL, "settings_provider_error", [])


def test_provider_error_reports_invalid_request_layers():
    settings = {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR}
    result = resolve_test_scope(settings=settings, plan_value="partial")

    assert (_resolve_triple(result), result["invalid_layers"]) == (
        (TEST_SCOPE_FULL, "settings_provider_error", []),
        ["plan"],
    )


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="empty-dict"),
        pytest.param(None, id="settings-none"),
    ],
)
def test_empty_provider_answer_is_not_an_error(settings):
    result = resolve_test_scope(settings=settings, plan_value="scoped")

    assert _resolve_triple(result) == (TEST_SCOPE_SCOPED, "plan", [])
