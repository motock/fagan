"""Model and forge credentials resolve per namespace (px-03).

The default namespace keeps reading the process environment. Any other bound
namespace never does: its credential comes only from the credentials plugin
seam, and when the seam has nothing for it the refusal is explicit
(``credential_missing``), never a fallback to an environment variable.

Credential values here are fixed fake strings. They appear only in fixtures
and ``==`` comparisons, never in an assertion message.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.backend_claude import _first_party_claude_env
from app.credentials import (
    CredentialError,
    CredentialMissing,
    CredentialRef,
    SecretValue,
    is_default_namespace,
    resolve,
    resolve_for_injection,
)
from app.inference_providers import ollama_auth_kwargs
from pipeline.actor import bind_namespace
from pipeline.execution import _local_agent_env
from pipeline.store_records import DEFAULT_NAMESPACE

OTHER = "alpha"
ANOTHER = "beta"
OLLAMA_ENV = "OLLAMA_API_KEY"
GH_ENV = "GH_TOKEN"
ENV_VALUE = "process-environment-value"
PLUGIN_VALUE = "plugin-value-for-alpha"
PLUGIN_FORGE_VALUE = "plugin-forge-value-for-alpha"

_RESET = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OLLAMA_API_KEY",
    "PIPELINE_ANTHROPIC_API_KEY",
    "PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV",
    "PIPELINE_CREDENTIALS_PLUGIN",
    "PIPELINE_CREDENTIALS_PLUGINS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _RESET:
        monkeypatch.delenv(var, raising=False)


def _write_namespace_plugin(tmp_path, monkeypatch) -> str:
    """Install a credentials plugin that only knows namespace ``alpha``.

    It returns a model key and a forge token for ``alpha`` and nothing for any
    other namespace. The module name is unique per call so the seam's
    per-spec provider cache cannot leak one test's plugin into another.
    """
    name = f"px03_plugin_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(
        "from app.credentials import Credential, SecretValue\n"
        "_VALUES = {\n"
        f"    ('model', 'ollama_api_key'): {PLUGIN_VALUE!r},\n"
        f"    ('forge', 'github_token'): {PLUGIN_FORGE_VALUE!r},\n"
        "}\n"
        "class _Provider:\n"
        "    def get(self, ref):\n"
        "        if ref.namespace != 'alpha':\n"
        "            return None\n"
        "        value = _VALUES.get((ref.purpose, ref.name))\n"
        "        return None if value is None else Credential(SecretValue(value))\n"
        "def factory():\n"
        "    return _Provider()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PIPELINE_CREDENTIALS_PLUGIN", f"{name}:factory")
    return name


# -- the default namespace is unchanged --------------------------------------


def test_default_namespace_reads_the_ollama_key_from_the_environment() -> None:
    value = resolve(
        CredentialRef("model", "ollama_api_key", namespace=DEFAULT_NAMESPACE),
        environ={OLLAMA_ENV: ENV_VALUE},
    )

    assert value is not None
    assert value.reveal() == ENV_VALUE


def test_default_namespace_unset_model_key_is_none_not_an_error() -> None:
    assert (
        resolve(
            CredentialRef("model", "ollama_api_key", namespace=DEFAULT_NAMESPACE),
            environ={},
        )
        is None
    )


def test_default_namespace_is_the_store_default_namespace() -> None:
    assert is_default_namespace(DEFAULT_NAMESPACE)
    assert not is_default_namespace(OTHER)


def test_default_namespace_ollama_header_is_sent_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OLLAMA_ENV, " k ")

    assert ollama_auth_kwargs() == {"headers": {"Authorization": "Bearer k"}}


def test_default_namespace_child_env_strips_the_process_forge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GH_ENV, ENV_VALUE)

    env = _local_agent_env(None)

    assert GH_ENV not in env


# -- another namespace ignores the process environment ------------------------


@pytest.mark.parametrize(
    ("purpose", "name", "variable"),
    [
        pytest.param("model", "ollama_api_key", OLLAMA_ENV, id="model-key"),
        pytest.param("forge", "github_token", GH_ENV, id="forge-token"),
    ],
)
def test_other_namespace_ignores_a_set_environment_variable(
    purpose: str, name: str, variable: str
) -> None:
    with pytest.raises(CredentialMissing, match="credential_missing") as info:
        resolve(
            CredentialRef(purpose, name, namespace=OTHER),
            environ={variable: ENV_VALUE},
        )

    assert ENV_VALUE not in str(info.value)


def test_other_namespace_ollama_header_is_not_sent_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OLLAMA_ENV, ENV_VALUE)

    with bind_namespace(OTHER):
        assert ollama_auth_kwargs() == {}


def test_other_namespace_child_env_drops_the_process_forge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GH_ENV, ENV_VALUE)

    with bind_namespace(OTHER):
        env = _local_agent_env(None)

    assert GH_ENV not in env
    assert ENV_VALUE not in env.values()


def test_other_namespace_child_env_drops_the_process_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", ENV_VALUE)

    with bind_namespace(OTHER):
        env = _first_party_claude_env()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_default_namespace_child_env_keeps_the_process_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", ENV_VALUE)

    env = _first_party_claude_env()

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ENV_VALUE


# -- a missing reference is an explicit denial --------------------------------


def test_other_namespace_without_a_plugin_is_credential_missing() -> None:
    with pytest.raises(CredentialMissing) as info:
        resolve(CredentialRef("model", "ollama_api_key", namespace=OTHER), environ={})

    assert str(info.value) == "credential_missing"
    assert isinstance(info.value, CredentialError)


def test_resolve_for_injection_refuses_outside_the_default_namespace() -> None:
    with pytest.raises(CredentialMissing):
        resolve_for_injection(
            CredentialRef("forge", "github_token", namespace=OTHER), environ={}
        )


# -- a plugin supplies another namespace's credential ------------------------


def test_plugin_supplies_the_credential_for_its_namespace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_namespace_plugin(tmp_path, monkeypatch)

    with bind_namespace(OTHER):
        assert ollama_auth_kwargs() == {
            "headers": {"Authorization": f"Bearer {PLUGIN_VALUE}"}
        }


def test_plugin_forge_token_reaches_only_its_namespace_child_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_namespace_plugin(tmp_path, monkeypatch)
    monkeypatch.setenv(GH_ENV, ENV_VALUE)

    with bind_namespace(OTHER):
        env = _local_agent_env(None)
    with bind_namespace(ANOTHER):
        other_env = _local_agent_env(None)

    assert env[GH_ENV] == PLUGIN_FORGE_VALUE
    assert ENV_VALUE not in env.values()
    assert GH_ENV not in other_env


def test_one_namespace_cannot_resolve_another_namespaces_reference(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_namespace_plugin(tmp_path, monkeypatch)

    with pytest.raises(CredentialMissing):
        resolve(CredentialRef("model", "ollama_api_key", namespace=ANOTHER))
    with pytest.raises(CredentialMissing):
        resolve(CredentialRef("forge", "github_token", namespace=ANOTHER))


def test_plugin_result_is_still_wrapped_in_a_secret_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_namespace_plugin(tmp_path, monkeypatch)

    value = resolve(CredentialRef("model", "ollama_api_key", namespace=OTHER))

    assert isinstance(value, SecretValue)
    assert repr(value) == "<redacted>"


# -- a two-segment namespace is an opaque key ---------------------------------

ACME_WEB = "acme/web"
TWO_SEGMENT_VALUE = "two-segment-value-for-acme-web"


@pytest.mark.parametrize(
    "namespace",
    [
        pytest.param("acme/web", id="acme-web"),
        pytest.param("a-b/c-d", id="hyphenated-segments"),
    ],
)
def test_credential_ref_keeps_a_two_segment_namespace_verbatim(namespace) -> None:
    ref = CredentialRef("model", "ollama_api_key", namespace=namespace)

    assert ref.namespace == namespace


@pytest.mark.parametrize(
    "namespace",
    [
        pytest.param("default", id="default"),
        pytest.param("tenant-a", id="single-segment"),
        pytest.param("a.b", id="dotted-single-segment"),
    ],
)
def test_credential_ref_still_accepts_a_single_segment_namespace(namespace) -> None:
    ref = CredentialRef("model", "ollama_api_key", namespace=namespace)

    assert ref.namespace == namespace


@pytest.mark.parametrize(
    "namespace",
    [
        pytest.param("a/b/c", id="three-segments"),
        pytest.param("/b", id="leading-separator"),
        pytest.param("a/", id="trailing-separator"),
        pytest.param("a/b/", id="trailing-separator-after-two-segments"),
        pytest.param("A/b", id="upper-case-first-segment"),
        pytest.param("a/B", id="upper-case-second-segment"),
        pytest.param("a//b", id="empty-middle-segment"),
        pytest.param("a b/c", id="space-in-segment"),
        pytest.param("../x", id="parent-traversal"),
        pytest.param("", id="empty"),
        pytest.param("a" * 65 + "/web", id="sixty-five-character-segment"),
    ],
)
def test_credential_ref_refuses_a_malformed_namespace(namespace) -> None:
    with pytest.raises(CredentialError) as excinfo:
        CredentialRef("model", "ollama_api_key", namespace=namespace)

    assert str(excinfo.value) == "invalid credential reference: namespace"


def test_credential_ref_refuses_a_none_namespace() -> None:
    with pytest.raises(CredentialError) as excinfo:
        CredentialRef("model", "ollama_api_key", namespace=None)

    assert str(excinfo.value) == "invalid credential reference: namespace"


def _write_two_segment_plugin(tmp_path, monkeypatch) -> None:
    """Install a credentials plugin that serves only the ``acme/web`` namespace.

    A namespace that reached the plugin altered would find nothing and the
    resolve would raise, so a passing resolve proves the key arrived unchanged.
    """
    name = f"nsref_plugin_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(
        "from app.credentials import Credential, SecretValue\n"
        "class _Provider:\n"
        "    def get(self, ref):\n"
        "        if ref.namespace != 'acme/web' or ref.name != 'ollama_api_key':\n"
        "            return None\n"
        f"        return Credential(SecretValue({TWO_SEGMENT_VALUE!r}))\n"
        "def factory():\n"
        "    return _Provider()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PIPELINE_CREDENTIALS_PLUGIN", f"{name}:factory")


def test_plugin_receives_a_two_segment_namespace_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_two_segment_plugin(tmp_path, monkeypatch)
    ref = CredentialRef("model", "ollama_api_key", namespace=ACME_WEB)

    value = resolve(ref)

    assert ref.namespace == ACME_WEB
    assert isinstance(value, SecretValue)
    assert value.reveal() == TWO_SEGMENT_VALUE


def test_two_segment_namespace_without_a_plugin_is_credential_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ref = CredentialRef("model", "ollama_api_key", namespace=ACME_WEB)

    with caplog.at_level(logging.INFO, logger="app.credentials"), pytest.raises(
        CredentialMissing
    ) as info:
        resolve(ref, environ={})

    assert str(info.value) == "credential_missing"
    records = [r for r in caplog.records if r.name == "app.credentials"]
    assert len(records) == 1
    assert records[0].credential_namespace == ACME_WEB
    assert records[0].credential_outcome == "missing"

