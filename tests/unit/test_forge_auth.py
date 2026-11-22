"""Tests for ``pipeline/forge_auth.env_kw``: the team forge environment.

A team namespace's pipeline-side git and gh commands carry the team's own
GitHub token in the child environment and never the process token. The
credential seam is stubbed at ``resolve_for_injection``; no test uses a real
token value.
"""

import os

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


def _operator_gh_login(tmp_path):
    """The operator's stored gh login: a hosts file that would answer for github.com."""
    operator_dir = tmp_path / "operator-gh"
    operator_dir.mkdir()
    (operator_dir / "hosts.yml").write_text(
        "github.com:\n    oauth_token: operator-stored-login\n    user: operator\n"
    )
    return operator_dir


def test_team_namespace_points_gh_at_an_empty_config_dir(seam):
    with bind_namespace(TEAM):
        config_dir = forge_auth.env_kw({})["env"]["GH_CONFIG_DIR"]
    assert os.path.isdir(config_dir)
    assert os.listdir(config_dir) == []


def test_team_namespace_without_credential_cannot_reach_the_operator_login(
    monkeypatch, tmp_path
):
    operator_dir = _operator_gh_login(tmp_path)
    monkeypatch.setenv("GH_CONFIG_DIR", str(operator_dir))

    def _missing(ref, **_kwargs):
        raise CredentialMissing("credential_missing")

    monkeypatch.setattr(forge_auth, "resolve_for_injection", _missing)
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({})["env"]

    assert env["GH_CONFIG_DIR"] != str(operator_dir)
    assert "GH_TOKEN" not in env
    assert os.listdir(env["GH_CONFIG_DIR"]) == []


def test_team_namespace_ignores_the_operator_global_and_system_git_config(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({})["env"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_team_namespace_isolation_holds_with_a_team_token(seam):
    with bind_namespace(TEAM):
        env = forge_auth.env_kw({})["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN
    assert os.listdir(env["GH_CONFIG_DIR"]) == []


def test_default_namespace_keeps_the_operator_login(monkeypatch, tmp_path):
    operator_dir = _operator_gh_login(tmp_path)
    monkeypatch.setenv("GH_CONFIG_DIR", str(operator_dir))
    with bind_namespace("default"):
        assert forge_auth.env_kw() == {}
