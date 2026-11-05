"""The model credential is read through the credential seam, never ambiently.

``app.backend_claude`` is the one place a model credential is revealed for a
dispatched child. These tests pin the seam behaviour: the default (no plugin)
keeps today's ``PIPELINE_ANTHROPIC_API_KEY``/``CLAUDE_CODE_OAUTH_TOKEN``
behaviour byte-for-byte, an operator plugin supplies the values instead, a
malformed plugin value is still refused with the pinned message, the
``PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV`` opt-out never consults the provider, and
a failing provider fails closed rather than falling back to the ambient
environment.

Credential values live only in this file's inputs and in ``assert ... == ...``
comparisons. They never appear in a test id, an assertion message, an exception
message or a log line, so a failure cannot print one.
"""
import importlib
import logging
import os
import textwrap

import pytest

from app.backend_claude import _first_party_claude_env
from app.credentials import CredentialError
from pipeline import execution

# The same clean-environment fixture as
# ``tests/unit/test_backend_claude_operator_key.py``: every variable the claude
# driver reacts to is removed, so the ambient shell cannot decide a result.
_ENV_VARS_TO_RESET = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "PIPELINE_ANTHROPIC_API_KEY", "PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PIPELINE_CREDENTIALS_PLUGIN", "PIPELINE_CREDENTIALS_PLUGINS",
    "PIPELINE_PLUGIN_MODULES",
    "PIPELINE_MODEL_REGISTRY_PATH",
    # The agent denylist: an inherited value would make the "none of these
    # reach the child" assertion depend on the developer's shell.
    *sorted(execution._AGENT_SECRET_ENV),
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS_TO_RESET:
        monkeypatch.delenv(var, raising=False)


def _write_plugin(tmp_path, filename: str, source: str) -> str:
    """Write a credentials plugin module into ``tmp_path`` and return its name.

    Each test uses a unique ``filename``: the credential seam caches its
    provider per spec string, so a reused name would hand a later test an
    earlier test's provider from ``sys.modules``/``_PLUGIN_PROVIDERS``.
    """
    (tmp_path / f"{filename}.py").write_text(
        textwrap.dedent(source).lstrip(), encoding="utf-8"
    )
    return filename


def _select_plugin(monkeypatch, tmp_path, name: str) -> str:
    """Make ``name:factory`` the configured credentials plugin."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PIPELINE_CREDENTIALS_PLUGIN", f"{name}:factory")
    return name


# ---------------------------------------------------------------------------
# Default provider: today's behaviour must not change
# ---------------------------------------------------------------------------


def test_default_provider_passes_the_ambient_operator_key_as_api_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test-0001")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test-0001"
    assert "PIPELINE_ANTHROPIC_API_KEY" not in env


def test_default_provider_adds_no_model_credential_when_none_is_configured():
    env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ---------------------------------------------------------------------------
# Operator plugin supplies the credential
# ---------------------------------------------------------------------------


def test_plugin_supplied_key_is_passed_as_api_key(monkeypatch, tmp_path):
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_key",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                if ref.name == "anthropic_api_key":
                    return creds.Credential(creds.SecretValue("sk-test-0001"))
                return None

        def factory():
            return Provider()
        """,
    )
    _select_plugin(monkeypatch, tmp_path, name)

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test-0001"
    assert "PIPELINE_ANTHROPIC_API_KEY" not in env


def test_plugin_supplied_malformed_key_is_refused_without_echoing_it(
    monkeypatch, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_malformed",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                if ref.name == "anthropic_api_key":
                    # An interior space survives .strip(), so the existing
                    # whitespace validation must still reject it.
                    return creds.Credential(creds.SecretValue("sk-test 0001"))
                return None

        def factory():
            return Provider()
        """,
    )
    _select_plugin(monkeypatch, tmp_path, name)

    with pytest.raises(RuntimeError) as excinfo:
        _first_party_claude_env()

    assert "malformed" in str(excinfo.value)
    assert "sk-test" not in str(excinfo.value)
    assert "sk-test" not in caplog.text


def test_plugin_supplied_oauth_token_wins_and_leaves_no_state(
    monkeypatch, tmp_path
):
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_oauth",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                if ref.name == "claude_oauth_token":
                    return creds.Credential(creds.SecretValue("sk-test-0001"))
                return None

        def factory():
            return Provider()
        """,
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-other-0002")
    _select_plugin(monkeypatch, tmp_path, name)

    env = _first_party_claude_env()

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-test-0001"
    # The injection goes into the returned child env only: the orchestrator's
    # own environment must not be rewritten (a later call would inherit it).
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-other-0002"

    # Second call, plugin unset and no ambient value: nothing may persist.
    monkeypatch.delenv("PIPELINE_CREDENTIALS_PLUGIN")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    second = _first_party_claude_env()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in second


def test_plugin_supplied_credentials_do_not_expose_agent_secret_names(
    monkeypatch, tmp_path
):
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_agent_names",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                if ref.name == "anthropic_api_key":
                    return creds.Credential(creds.SecretValue("sk-test-0001"))
                if ref.name == "claude_oauth_token":
                    return creds.Credential(creds.SecretValue("sk-test-0001"))
                return None

        def factory():
            return Provider()
        """,
    )
    _select_plugin(monkeypatch, tmp_path, name)

    env = _first_party_claude_env()

    leaked = sorted(set(execution._AGENT_SECRET_ENV) & set(env))
    assert leaked == []


# ---------------------------------------------------------------------------
# Failure paths: never consult the provider when opted out, never fall back
# ---------------------------------------------------------------------------


def test_provider_opt_out_skips_the_provider_and_inherits_everything(
    monkeypatch, tmp_path
):
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_opted_out",
        """
        import app.credentials as creds

        calls = []

        class Provider:
            def get(self, ref):
                calls.append(ref)
                raise creds.CredentialError("provider must not run")

        def factory():
            return Provider()
        """,
    )
    monkeypatch.setenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", "1")
    _select_plugin(monkeypatch, tmp_path, name)
    # Import the plugin up front so the counter exists: an empty list proves
    # the opt-out branch never constructed or consulted the provider.
    module = importlib.import_module(name)

    env = _first_party_claude_env()

    assert module.calls == []
    # The opt-out branch is today's full inheritance, unchanged.
    assert env == dict(os.environ)


def test_failing_provider_fails_closed_without_ambient_fallback(
    monkeypatch, tmp_path
):
    name = _write_plugin(
        tmp_path,
        "cred_plugin_model_failing",
        """
        class Provider:
            def get(self, ref):
                raise RuntimeError("secret store unavailable")

        def factory():
            return Provider()
        """,
    )
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-ambient-0002")
    _select_plugin(monkeypatch, tmp_path, name)

    with pytest.raises(CredentialError) as excinfo:
        _first_party_claude_env()

    # Fail closed: the ambient operator key must not be used as a fallback, and
    # the error must not carry a credential value either.
    assert "sk-ambient-0002" not in str(excinfo.value)
