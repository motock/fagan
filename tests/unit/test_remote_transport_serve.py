"""Tests for the remote MCP transport entry point: fail-closed settings + serve."""

from __future__ import annotations

import inspect
import sys
import types
from unittest.mock import Mock

import pytest

import app.pipeline_mcp_server
from app.oidc import IDENTITY_PROVIDER_ENV, OidcProvider
from pipeline import remote_transport as rt

TRANSPORT_ENV = "PIPELINE_MCP_TRANSPORT"
HOST_ENV = "PIPELINE_MCP_HOST"
PORT_ENV = "PIPELINE_MCP_PORT"


class FakeMCP:
    """Records which SDK app factory ``serve`` reaches for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def streamable_http_app(self, host: str | None = None) -> object:
        self.calls.append(("streamable_http_app", host))
        return object()

    def sse_app(self, host: str | None = None) -> object:
        self.calls.append(("sse_app", host))
        return object()


class GuardedMCP:
    """Explodes if ``serve`` builds the ASGI app before the OIDC guard passes."""

    def streamable_http_app(self, host: str | None = None) -> object:
        raise AssertionError("app built before OIDC guard")

    def sse_app(self, host: str | None = None) -> object:
        raise AssertionError("app built before OIDC guard")


def _serve_with_fakes(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> tuple[FakeMCP, list[tuple[tuple[object, ...], dict[str, object]]]]:
    """Run ``serve`` with a fake SDK app factory, provider and uvicorn."""
    mcp = FakeMCP()
    uvicorn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _run(*args: object, **kwargs: object) -> None:
        uvicorn_calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=_run))
    monkeypatch.setattr(rt, "build_identity_provider", lambda *a, **k: Mock(spec=OidcProvider))
    rt.serve(mcp, environ=env)
    return mcp, uvicorn_calls


REMOTE_ENV = {TRANSPORT_ENV: "streamable-http", IDENTITY_PROVIDER_ENV: "oidc"}


def test_settings_defaults_to_loopback_and_8000() -> None:
    env = {TRANSPORT_ENV: "streamable-http", IDENTITY_PROVIDER_ENV: "oidc"}
    assert rt._settings_from_env(env) == ("streamable-http", "127.0.0.1", 8000)


def test_settings_accepts_explicit_host_and_port() -> None:
    env = {
        TRANSPORT_ENV: "streamable-http",
        IDENTITY_PROVIDER_ENV: "oidc",
        HOST_ENV: "0.0.0.0",
        PORT_ENV: "9000",
    }
    assert rt._settings_from_env(env) == ("streamable-http", "0.0.0.0", 9000)


def test_settings_rejects_stdio_transport() -> None:
    env = {TRANSPORT_ENV: "stdio", IDENTITY_PROVIDER_ENV: "oidc"}
    with pytest.raises(SystemExit):
        rt._settings_from_env(env)


def test_settings_rejects_unknown_transport() -> None:
    env = {TRANSPORT_ENV: "http", IDENTITY_PROVIDER_ENV: "oidc"}
    with pytest.raises(SystemExit):
        rt._settings_from_env(env)


def test_settings_fails_closed_without_oidc() -> None:
    env = {TRANSPORT_ENV: "streamable-http"}
    with pytest.raises(SystemExit, match="OIDC"):
        rt._settings_from_env(env)


def test_settings_rejects_non_oidc_provider() -> None:
    env = {TRANSPORT_ENV: "streamable-http", IDENTITY_PROVIDER_ENV: "shared-secret"}
    with pytest.raises(SystemExit):
        rt._settings_from_env(env)


def test_settings_rejects_non_integer_port() -> None:
    env = {**REMOTE_ENV, PORT_ENV: "abc"}
    with pytest.raises(SystemExit):
        rt._settings_from_env(env)


@pytest.mark.parametrize("port", ["0", "70000"])
def test_settings_rejects_out_of_range_ports(port: str) -> None:
    env = {**REMOTE_ENV, PORT_ENV: port}
    with pytest.raises(SystemExit):
        rt._settings_from_env(env)


def test_serve_builds_streamable_http_app(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp, _ = _serve_with_fakes(monkeypatch, dict(REMOTE_ENV))
    assert mcp.calls[0][0] == "streamable_http_app"


def test_serve_wraps_app_in_bearer_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    _, uvicorn_calls = _serve_with_fakes(monkeypatch, dict(REMOTE_ENV))
    assert isinstance(uvicorn_calls[0][0][0], rt.BearerAuthMiddleware)


def test_serve_uses_sse_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SSE app must also receive the configured host: mcp 2.3.0 defaults
    # sse_app's host to 127.0.0.1 and enables its loopback-only DNS-rebinding
    # protection, which 400s every request that arrives via the operator's
    # reverse proxy on a wildcard bind.
    env = {**REMOTE_ENV, TRANSPORT_ENV: "sse", HOST_ENV: "0.0.0.0"}
    mcp, _ = _serve_with_fakes(monkeypatch, env)
    assert mcp.calls[0] == ("sse_app", "0.0.0.0")


def test_serve_passes_host_and_port_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    _, uvicorn_calls = _serve_with_fakes(
        monkeypatch, {**REMOTE_ENV, HOST_ENV: "0.0.0.0", PORT_ENV: "9000"}
    )
    assert uvicorn_calls[0][1] == {"host": "0.0.0.0", "port": 9000, "log_level": "info"}


def test_serve_fails_closed_before_building_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))
    monkeypatch.setattr(rt, "build_identity_provider", lambda *a, **k: object())
    with pytest.raises(SystemExit):
        rt.serve(GuardedMCP(), environ=dict(REMOTE_ENV))


def test_launch_shim_defaults_to_stdio() -> None:
    source = inspect.getsource(app.pipeline_mcp_server)
    assert source.index("mcp.run()") < source.index(
        "from pipeline.remote_transport import serve"
    )
