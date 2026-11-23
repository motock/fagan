import pytest

from pipeline.settings_registry import REGISTRY, accepts_github_issues

VALID_ISSUES = {
    "enabled": True,
    "repo_url": "https://github.com/acme/widgets",
    "label": "fagan",
    "poll_seconds": 300,
    "token_ref": "",
}


def test_github_issues_is_a_team_only_pref_with_an_empty_safe_value():
    key = REGISTRY["github_issues"]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "pref",
        "github_issues",
        {},
        ("team",),
    )


def test_github_issues_accepts_a_well_formed_value_with_the_team_token():
    assert REGISTRY["github_issues"].accepts(VALID_ISSUES) is True


def test_github_issues_accepts_a_separate_secret_reference_token():
    value = {**VALID_ISSUES, "token_ref": "secret://file/github-issues-token"}
    assert REGISTRY["github_issues"].accepts(value) is True


def test_github_issues_accepts_its_empty_safe_value():
    assert REGISTRY["github_issues"].accepts({}) is True


@pytest.mark.parametrize("poll_seconds", [60, 3600])
def test_github_issues_accepts_poll_seconds_at_the_bounds(poll_seconds):
    assert accepts_github_issues({**VALID_ISSUES, "poll_seconds": poll_seconds}) is True


@pytest.mark.parametrize("poll_seconds", [59, 3601, 0, -60, True, "300", 300.0])
def test_github_issues_refuses_poll_seconds_outside_the_bounds_or_wrong_type(
    poll_seconds,
):
    assert (
        accepts_github_issues({**VALID_ISSUES, "poll_seconds": poll_seconds}) is False
    )


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://ghe.acme.com/acme/widgets",
        "https://github.com.evil.test/acme/widgets",
        "https://gitlab.com/acme/widgets",
        "https://user:secret@github.com/acme/widgets",
        "https://github.com/acme/widgets?token=x",
        "http://github.com/acme/widgets",
        "https://github.com/",
    ],
)
def test_github_issues_refuses_a_repo_url_that_is_not_a_plain_github_com_url(repo_url):
    assert accepts_github_issues({**VALID_ISSUES, "repo_url": repo_url}) is False


@pytest.mark.parametrize("label", ["", "has space", "a/b", "x" * 51, "ünicode"])
def test_github_issues_refuses_a_malformed_label(label):
    assert accepts_github_issues({**VALID_ISSUES, "label": label}) is False


def test_github_issues_accepts_a_label_of_fifty_characters():
    assert accepts_github_issues({**VALID_ISSUES, "label": "x" * 50}) is True


@pytest.mark.parametrize(
    "token_ref",
    ["ghp_plaintexttoken", "secret://", "secret:///github-issues", "env://GH_TOKEN"],
)
def test_github_issues_refuses_a_token_ref_that_is_neither_empty_nor_a_secret_reference(
    token_ref,
):
    assert accepts_github_issues({**VALID_ISSUES, "token_ref": token_ref}) is False


def test_github_issues_refuses_a_non_boolean_enabled():
    assert accepts_github_issues({**VALID_ISSUES, "enabled": "yes"}) is False


def test_github_issues_refuses_an_extra_field():
    assert (
        accepts_github_issues({**VALID_ISSUES, "api_base": "https://x.test"}) is False
    )


@pytest.mark.parametrize("missing", sorted(VALID_ISSUES))
def test_github_issues_refuses_a_missing_field(missing):
    value = {name: v for name, v in VALID_ISSUES.items() if name != missing}
    assert accepts_github_issues(value) is False


@pytest.mark.parametrize("value", [None, "", [], "https://github.com/acme/widgets"])
def test_github_issues_refuses_a_value_that_is_not_a_dict(value):
    assert accepts_github_issues(value) is False
