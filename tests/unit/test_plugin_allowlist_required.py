"""The identity and authorizer seams require the module allowlist.

``app/plugin_loader`` documents ``PIPELINE_PLUGIN_MODULES`` as required on any
host where more than one principal can write the tree. The identity and
authorizer seams are the security-sensitive pair: the authorizer decides every
ADR 0009 action and the identity provider decides who the caller is, so an
operator who can write the tree must not be able to install either one without
that allowlist.

:func:`app.plugin_loader.require_allowlist` is the shared, import-free check both
seams use. These tests write throwaway plugin modules into ``tmp_path`` and pass
an explicit ``environ`` dict (or ``monkeypatch.setenv`` for the authorizer, which
reads ``os.environ``), so the real process environment never decides the result.
Every refusal assertion also proves the plugin module was NOT imported.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

import app.policy
from app.identity import IdentityError, SharedSecretProvider
from app.oidc import build_identity_provider
from app.plugin_loader import (
    ALLOWLIST_ENV,
    PluginError,
    plugin_env_name,
    require_allowlist,
)
from app.policy import LocalPolicy, default_authorizer

IDENTITY_PLUGIN_ENV = plugin_env_name("identity")
AUTHORIZER_PLUGIN_ENV = plugin_env_name("authorizer")

#: A provider module: the factory ``build_identity_provider`` resolves.
_PROVIDER_SOURCE = """
class PluginProvider:
    network_grade = True

    def __init__(self, secret_reader, env):
        self.secret_reader = secret_reader
        self.env = env

    def authenticate(self, credential):
        return None


def make(secret_reader, env):
    return PluginProvider(secret_reader, env)
"""

#: An authorizer module: the factory ``default_authorizer`` resolves.
_AUTHORIZER_SOURCE = """
class PluginAuthorizer:
    def allow(self, principal, action, resource=None):
        return True


def build():
    return PluginAuthorizer()
"""


def _secret_reader() -> str:
    return "correct-horse-battery-staple"


def _write_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, source: str
) -> str:
    """Write ``source`` as module ``name`` on ``sys.path``; return the name."""
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    return name


@pytest.fixture(autouse=True)
def clear_authorizer_cache() -> None:
    """Isolate every test from the process-wide authorizer plugin cache."""
    app.policy._PLUGIN_AUTHORIZERS.clear()
    yield
    app.policy._PLUGIN_AUTHORIZERS.clear()


# --- the shared helper ------------------------------------------------------


def test_no_plugin_configured_returns_none_whatever_the_allowlist() -> None:
    assert require_allowlist("identity", {}) is None
    assert require_allowlist("identity", {ALLOWLIST_ENV: ""}) is None
    assert require_allowlist("identity", {ALLOWLIST_ENV: "some_module"}) is None
    assert require_allowlist("authorizer", {AUTHORIZER_PLUGIN_ENV: "   "}) is None


def test_missing_allowlist_names_both_variables() -> None:
    env = {IDENTITY_PLUGIN_ENV: "some_plugin:make"}

    with pytest.raises(PluginError) as excinfo:
        require_allowlist("identity", env)

    message = str(excinfo.value)
    assert IDENTITY_PLUGIN_ENV in message
    assert ALLOWLIST_ENV in message

    with pytest.raises(PluginError) as excinfo:
        require_allowlist("authorizer", {AUTHORIZER_PLUGIN_ENV: "some_plugin:make"})

    message = str(excinfo.value)
    assert AUTHORIZER_PLUGIN_ENV in message
    assert ALLOWLIST_ENV in message


@pytest.mark.parametrize("blank", ["", "   ", ",", " , , ", ",\t,\n"])
def test_blank_allowlist_is_treated_as_unset(blank: str) -> None:
    env = {IDENTITY_PLUGIN_ENV: "some_plugin:make", ALLOWLIST_ENV: blank}

    with pytest.raises(PluginError) as excinfo:
        require_allowlist("identity", env)

    assert ALLOWLIST_ENV in str(excinfo.value)


def test_set_allowlist_permits_the_seam() -> None:
    env = {IDENTITY_PLUGIN_ENV: "some_plugin:make", ALLOWLIST_ENV: "some_plugin"}

    assert require_allowlist("identity", env) is None


def test_refusal_never_names_the_spec_or_the_module() -> None:
    env = {AUTHORIZER_PLUGIN_ENV: "secret_backend_xyz:build"}

    with pytest.raises(PluginError) as excinfo:
        require_allowlist("authorizer", env)

    message = str(excinfo.value)
    assert "secret_backend_xyz" not in message
    assert "build" not in message


def test_helper_does_not_import_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "allowlist_helper_never_imports"
    _write_module(tmp_path, monkeypatch, name, _AUTHORIZER_SOURCE)
    env = {AUTHORIZER_PLUGIN_ENV: f"{name}:build"}

    with pytest.raises(PluginError):
        require_allowlist("authorizer", env)

    assert name not in sys.modules


# --- the identity seam ------------------------------------------------------


def test_identity_plugin_without_allowlist_is_refused_and_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "allowlist_identity_refused"
    _write_module(tmp_path, monkeypatch, name, _PROVIDER_SOURCE)
    env = {IDENTITY_PLUGIN_ENV: f"{name}:make"}

    with pytest.raises(IdentityError) as excinfo:
        build_identity_provider(_secret_reader, env)

    assert ALLOWLIST_ENV in str(excinfo.value)
    assert name not in sys.modules


def test_identity_plugin_with_allowlist_builds_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "allowlist_identity_permitted"
    _write_module(tmp_path, monkeypatch, name, _PROVIDER_SOURCE)
    env = {IDENTITY_PLUGIN_ENV: f"{name}:make", ALLOWLIST_ENV: name}

    provider = build_identity_provider(_secret_reader, env)

    assert type(provider).__name__ == "PluginProvider"
    assert not isinstance(provider, SharedSecretProvider)


# --- the authorizer seam ----------------------------------------------------


def test_authorizer_plugin_without_allowlist_is_refused_and_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "allowlist_authorizer_refused"
    _write_module(tmp_path, monkeypatch, name, _AUTHORIZER_SOURCE)
    monkeypatch.setenv(AUTHORIZER_PLUGIN_ENV, f"{name}:build")
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

    with pytest.raises(PluginError) as excinfo:
        default_authorizer()

    assert ALLOWLIST_ENV in str(excinfo.value)
    assert name not in sys.modules


# --- defaults stay permissive ----------------------------------------------


def test_unconfigured_seams_keep_their_builtin_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(IDENTITY_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(AUTHORIZER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

    assert isinstance(build_identity_provider(_secret_reader, {}), SharedSecretProvider)
    assert isinstance(default_authorizer(), LocalPolicy)
