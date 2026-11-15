"""Model and forge credentials are read per namespace, never from the environment
of another namespace.

The default namespace keeps today's process-environment behaviour. A bound
non-default namespace must not see a process-environment credential: the
``claude`` child is built from the operator key read for the bound namespace,
so an environment variable set for the default namespace never reaches it.

Credential values here are fixed fake strings. They appear only in the
environment fixtures and in ``==`` comparisons, never in an assertion message.
"""

import pytest

from app.backend_claude import _first_party_claude_env, _validated_operator_key
from pipeline.actor import bind_namespace

OPERATOR_KEY = "operator-key-for-namespace-tests"
OTHER_NAMESPACE = "alpha"

_ENV_VARS_TO_RESET = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "PIPELINE_ANTHROPIC_API_KEY",
    "PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PIPELINE_CREDENTIALS_PLUGIN",
    "PIPELINE_CREDENTIALS_PLUGINS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS_TO_RESET:
        monkeypatch.delenv(var, raising=False)


def test_default_namespace_reads_the_operator_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbound (default namespace) the environment key is still read, as before."""
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", OPERATOR_KEY)

    assert _validated_operator_key() == OPERATOR_KEY


def test_other_namespace_ignores_the_operator_key_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound non-default namespace gets no key from the process environment."""
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", OPERATOR_KEY)

    with bind_namespace(OTHER_NAMESPACE):
        assert _validated_operator_key() == ""


def test_other_namespace_child_environment_carries_no_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``claude`` child env built for a bound namespace has no env-sourced key."""
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", OPERATOR_KEY)

    with bind_namespace(OTHER_NAMESPACE):
        env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env
