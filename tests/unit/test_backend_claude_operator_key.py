"""An operator-supplied PIPELINE_ANTHROPIC_API_KEY reaches the `claude` CLI
as ANTHROPIC_API_KEY while every provider-redirect var stays stripped."""
import pytest

from app.backend_claude import _first_party_claude_env

_STRIPPED_VARS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "PIPELINE_ANTHROPIC_API_KEY", "PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _STRIPPED_VARS:
        monkeypatch.delenv(var, raising=False)


def test_should_pass_operator_key_as_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_should_strip_base_url_when_operator_key_set(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")

    env = _first_party_claude_env()

    assert "ANTHROPIC_BASE_URL" not in env


@pytest.mark.parametrize("var", [
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
])
def test_should_still_strip_redirect_var_when_operator_key_set(monkeypatch, var):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv(var, "redirect")

    env = _first_party_claude_env()

    assert var not in env


def test_should_strip_ambient_api_key_without_operator_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")

    env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env


def test_should_prefer_operator_key_over_ambient_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "op")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "op"


def test_should_strip_surrounding_whitespace_from_operator_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "  sk-test\n")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_should_not_add_api_key_for_blank_operator_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "   ")

    env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env


def test_should_pass_oauth_token_through_unchanged(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    env = _first_party_claude_env()

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
