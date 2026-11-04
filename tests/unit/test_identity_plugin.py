"""Tests for selecting the identity provider through the plugin loader.

A plugin is selected ONLY by ``PIPELINE_IDENTITY_PLUGIN=module:attr``, an
operator-controlled environment variable. These tests write throwaway plugin
modules into ``tmp_path`` and always pass an explicit ``environ`` dict, so the
real process environment never decides the outcome.
"""

from __future__ import annotations

import importlib
import textwrap

import pytest

from app.identity import IdentityError, SharedSecretProvider
from app.oidc import IDENTITY_PROVIDER_ENV, build_identity_provider
from pipeline import remote_transport as rt
from tests.unit.test_remote_transport_serve import _serve_with_fakes

IDENTITY_PLUGIN_ENV = "PIPELINE_IDENTITY_PLUGIN"
REMOTE_ENV = {"PIPELINE_MCP_TRANSPORT": "streamable-http", IDENTITY_PROVIDER_ENV: "oidc"}

#: A complete plugin module: a provider that records how it was constructed and
#: always denies, plus the factory ``build_identity_provider`` resolves.
_PROVIDER_SOURCE = """
from app.identity import IdentityError


class PluginProvider:
    network_grade = {network_grade}

    def __init__(self, secret_reader, env):
        self.secret_reader = secret_reader
        self.env = env

    def authenticate(self, credential):
        raise IdentityError("plugin denied this credential")


CALLS = []


def make(secret_reader, env):
    CALLS.append((secret_reader, env))
    return PluginProvider(secret_reader, env)
"""

#: A plugin whose factory returns an object without ``authenticate``.
_BROKEN_SOURCE = """
class NotAProvider:
    pass


def make(secret_reader, env):
    return NotAProvider()
"""


def _secret_reader() -> str:
    return "correct-horse-battery-staple"


def _write_plugin(
    tmp_path, monkeypatch: pytest.MonkeyPatch, name: str, source: str
) -> str:
    """Write ``source`` as module ``name`` on ``sys.path``; return its spec."""
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{name}:make"


def _provider_source(*, network_grade: bool) -> str:
    return _PROVIDER_SOURCE.format(network_grade=network_grade)


def test_no_plugin_keeps_the_shared_secret_default() -> None:
    provider = build_identity_provider(_secret_reader, {})

    assert isinstance(provider, SharedSecretProvider)


def test_plugin_factory_receives_secret_reader_and_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "identity_plugin_args"
    spec = _write_plugin(
        tmp_path, monkeypatch, name, _provider_source(network_grade=True)
    )
    module = importlib.import_module(name)
    env = {IDENTITY_PLUGIN_ENV: spec}

    provider = build_identity_provider(_secret_reader, env)

    assert module.CALLS == [(_secret_reader, env)]
    assert isinstance(provider, module.PluginProvider)
    assert provider.secret_reader is _secret_reader


@pytest.mark.parametrize("selector", [None, "shared-secret"])
def test_plugin_wins_over_the_builtin_selector(
    tmp_path, monkeypatch: pytest.MonkeyPatch, selector: str | None
) -> None:
    slug = "unset" if selector is None else selector.replace("-", "_")
    name = f"identity_plugin_wins_{slug}"
    spec = _write_plugin(
        tmp_path, monkeypatch, name, _provider_source(network_grade=True)
    )
    module = importlib.import_module(name)
    env = {IDENTITY_PLUGIN_ENV: spec}
    if selector is not None:
        env[IDENTITY_PROVIDER_ENV] = selector

    provider = build_identity_provider(_secret_reader, env)

    assert isinstance(provider, module.PluginProvider)
    assert not isinstance(provider, SharedSecretProvider)


def test_plugin_conflicting_with_oidc_selector_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_plugin(
        tmp_path,
        monkeypatch,
        "identity_plugin_conflict",
        _provider_source(network_grade=True),
    )
    env = {IDENTITY_PLUGIN_ENV: spec, IDENTITY_PROVIDER_ENV: "oidc"}

    with pytest.raises(IdentityError) as excinfo:
        build_identity_provider(_secret_reader, env)

    assert "conflicts" in str(excinfo.value)


@pytest.mark.parametrize(
    ("spec", "hides_spec"),
    [
        ("not-a-valid-plugin-spec", True),
        ("identity_plugin_missing_module_xyz:make", False),
    ],
)
def test_invalid_plugin_spec_raises_without_echoing_it(spec: str, hides_spec: bool) -> None:
    with pytest.raises(IdentityError) as excinfo:
        build_identity_provider(_secret_reader, {IDENTITY_PLUGIN_ENV: spec})

    message = str(excinfo.value)
    assert message.startswith("invalid identity plugin")
    if hides_spec:
        assert spec not in message


def test_factory_returning_object_without_authenticate_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_plugin(
        tmp_path, monkeypatch, "identity_plugin_no_authenticate", _BROKEN_SOURCE
    )

    with pytest.raises(IdentityError):
        build_identity_provider(_secret_reader, {IDENTITY_PLUGIN_ENV: spec})


def test_middleware_refuses_plugin_provider_without_network_grade(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_plugin(
        tmp_path,
        monkeypatch,
        "identity_plugin_local_grade",
        _provider_source(network_grade=False),
    )
    provider = build_identity_provider(_secret_reader, {IDENTITY_PLUGIN_ENV: spec})

    with pytest.raises(TypeError):
        rt.BearerAuthMiddleware(object(), provider)


def test_middleware_accepts_plugin_provider_declaring_network_grade(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_plugin(
        tmp_path,
        monkeypatch,
        "identity_plugin_remote_grade",
        _provider_source(network_grade=True),
    )
    provider = build_identity_provider(_secret_reader, {IDENTITY_PLUGIN_ENV: spec})

    middleware = rt.BearerAuthMiddleware(object(), provider)

    assert middleware is not None
    # The transport's own serve path builds the same gate via this helper, and
    # a network_grade-capable provider passes it.
    _mcp, uvicorn_calls = _serve_with_fakes(monkeypatch, REMOTE_ENV)
    assert isinstance(uvicorn_calls[0][0][0], rt.BearerAuthMiddleware)


def test_plugin_identity_error_passes_through(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_plugin(
        tmp_path,
        monkeypatch,
        "identity_plugin_denies",
        _provider_source(network_grade=True),
    )
    provider = build_identity_provider(_secret_reader, {IDENTITY_PLUGIN_ENV: spec})

    with pytest.raises(IdentityError) as excinfo:
        provider.authenticate("anything")

    assert str(excinfo.value) == "plugin denied this credential"
