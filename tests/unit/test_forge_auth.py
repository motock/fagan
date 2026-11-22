"""Tests for ``pipeline/forge_auth.env_kw``: the team forge environment.

A team namespace's pipeline-side git and gh commands carry the team's own
GitHub token in the child environment and never the process token. The
credential seam is stubbed at ``resolve_for_injection``; no test uses a real
token value.
"""

import pytest

from app.credentials import CredentialMissing, CredentialRef
from pipeline import forge_auth
from pipeline.actor import bind_namespace

TEAM = "acme/web"
TEAM_TOKEN = "team-token-for-tests"
CONFIG_ENTRIES = {
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
}


@pytest.fixture
def seam(monkeypatch):
    """Stub the credential seam; record every reference it is asked for."""
    asked = []

    def _stub(ref, **_kwargs):
        asked.append(ref)
        return TEAM_TOKEN

    monkeypatch.setattr(forge_auth, "resolve_for_injection", _stub)
    return asked


def test_default_namespace_without_base_returns_no_kwargs():
    with bind_namespace("default"):
        assert forge_auth.env_kw() == {}


def test_default_namespace_with_base_returns_base_unchanged(seam):
    base = {"GH_TOKEN": "process-token", "PATH": "/bin"}
    with bind_namespace("default"):
        assert forge_auth.env_kw(base) == {"env": base}
    assert seam == []


def test_team_namespace_sets_its_token_and_disables_terminal_prompts(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({"PATH": "/bin"})["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PATH"] == "/bin"


def test_team_namespace_clears_inherited_helpers_and_uses_gh_for_github(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({})["env"]
    assert env["GIT_CONFIG_COUNT"] == "2"
    for key, value in CONFIG_ENTRIES.items():
        assert env[key] == value


def test_team_namespace_without_credential_drops_process_tokens(monkeypatch):
    def _missing(ref, **_kwargs):
        raise CredentialMissing("credential_missing")

    monkeypatch.setattr(forge_auth, "resolve_for_injection", _missing)
    base = {
        "GH_TOKEN": "process-gh",
        "GITHUB_TOKEN": "process-github",
        "GH_ENTERPRISE_TOKEN": "process-ghe",
        "GITHUB_ENTERPRISE_TOKEN": "process-github-ghe",
    }
    with bind_namespace(TEAM):
        env = forge_auth.env_kw(base)["env"]
    for name in base:
        assert name not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_team_token_replaces_a_process_gh_token(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({"GH_TOKEN": "process-gh"})["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN


def test_existing_config_entries_are_kept_and_count_grows(seam):
    base = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.sslVerify",
        "GIT_CONFIG_VALUE_0": "true",
    }
    with bind_namespace(TEAM):
        env = forge_auth.env_kw(base)["env"]
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "http.sslVerify"
    assert env["GIT_CONFIG_VALUE_0"] == "true"
    assert env["GIT_CONFIG_KEY_1"] == CONFIG_ENTRIES["GIT_CONFIG_KEY_0"]
    assert env["GIT_CONFIG_KEY_2"] == CONFIG_ENTRIES["GIT_CONFIG_KEY_1"]


def test_non_integer_config_count_is_treated_as_zero(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({"GIT_CONFIG_COUNT": "many"})["env"]
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == CONFIG_ENTRIES["GIT_CONFIG_KEY_0"]


def test_seam_is_asked_for_the_bound_teams_forge_token(seam):
    with bind_namespace(TEAM):
        forge_auth.env_kw({})
    assert seam == [CredentialRef("forge", "github_token", namespace=TEAM)]


def test_unexpected_seam_error_propagates(monkeypatch):
    def _broken(ref, **_kwargs):
        raise RuntimeError("credential store unreachable")

    monkeypatch.setattr(forge_auth, "resolve_for_injection", _broken)
    with bind_namespace(TEAM), pytest.raises(RuntimeError, match="credential store unreachable"):
        forge_auth.env_kw({})


def test_base_mapping_is_not_mutated(seam):
    base = {"GH_TOKEN": "process-gh", "PATH": "/bin"}
    snapshot = dict(base)
    with bind_namespace(TEAM):
        forge_auth.env_kw(base)
    assert base == snapshot
