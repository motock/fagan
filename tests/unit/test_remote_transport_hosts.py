"""Tests for the remote MCP transport host allowlist and the enforcement refusal."""

from __future__ import annotations

import sys
import types
from unittest.mock import Mock

import pytest

from app.oidc import IDENTITY_PROVIDER_ENV, OidcProvider
from pipeline import remote_transport as rt

TRANSPORT_ENV = "PIPELINE_MCP_TRANSPORT"
HOST_ENV = "PIPELINE_MCP_HOST"
PORT_ENV = "PIPELINE_MCP_PORT"
ALLOWED_HOSTS_ENV = rt.ALLOWED_HOSTS_ENV
ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"


class FakeMCP:
    """Records the app-factory call and the ``transport_security`` it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, object]] = []
        self.middleware: list[object] = []

    def streamable_http_app(
        self, host: str | None = None, transport_security: object = None
    ) -> object:
        self.calls.append(("streamable_http_app", host, transport_security))
        return object()

    def sse_app(
        self, host: str | None = None, transport_security: object = None
    ) -> object:
        self.calls.append(("sse_app", host, transport_security))
        return object()


def _serve_with_fakes(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> tuple[FakeMCP, list[tuple[tuple[object, ...], dict[str, object]]]]:
    """Run ``serve`` with a fake SDK app factory, provider and uvicorn."""
    mcp = FakeMCP()
    uvicorn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _run(*args: object, **kwargs: object) -> None:
        uvicorn_calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=_run))
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: Mock(spec=OidcProvider)
    )
    rt.serve(mcp, environ=env)
    return mcp, uvicorn_calls


REMOTE_ENV = {
    TRANSPORT_ENV: "streamable-http",
    IDENTITY_PROVIDER_ENV: "oidc",
    ENFORCE_ENV: "1",
}


def test_transport_security_unset_is_none() -> None:
    assert rt._transport_security({}) is None


def test_transport_security_blank_is_none() -> None:
    assert rt._transport_security({ALLOWED_HOSTS_ENV: "  "}) is None


def test_transport_security_only_separators_is_none() -> None:
    assert rt._transport_security({ALLOWED_HOSTS_ENV: ", ,"}) is None


def test_transport_security_splits_and_strips_hosts() -> None:
    settings = rt._transport_security(
        {ALLOWED_HOSTS_ENV: "mcp.example.com, other.example:*"}
    )
    assert settings.allowed_hosts == ["mcp.example.com", "other.example:*"]


def test_transport_security_keeps_dns_rebinding_protection() -> None:
    settings = rt._transport_security({ALLOWED_HOSTS_ENV: "mcp.example.com"})
    assert settings.enable_dns_rebinding_protection is True


def test_transport_security_allows_no_browser_origins() -> None:
    settings = rt._transport_security({ALLOWED_HOSTS_ENV: "mcp.example.com"})
    assert settings.allowed_origins == []


def test_serve_passes_transport_security_to_streamable_http_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {**REMOTE_ENV, ALLOWED_HOSTS_ENV: "mcp.example.com, other.example:*"}
    mcp, _ = _serve_with_fakes(monkeypatch, env)
    assert mcp.calls[0] == (
        "streamable_http_app",
        "127.0.0.1",
        rt._transport_security(env),
    )


def test_serve_passes_transport_security_to_sse_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {**REMOTE_ENV, TRANSPORT_ENV: "sse", ALLOWED_HOSTS_ENV: "mcp.example.com"}
    mcp, _ = _serve_with_fakes(monkeypatch, env)
    assert mcp.calls[0] == ("sse_app", "127.0.0.1", rt._transport_security(env))


def test_serve_passes_none_when_allowlist_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp, _ = _serve_with_fakes(monkeypatch, dict(REMOTE_ENV))
    assert mcp.calls[0][2] is None


def test_serve_refuses_to_start_with_enforcement_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {**REMOTE_ENV, ENFORCE_ENV: "0", ALLOWED_HOSTS_ENV: "mcp.example.com"}
    mcp = FakeMCP()
    monkeypatch.setitem(
        sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None)
    )
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: Mock(spec=OidcProvider)
    )
    with pytest.raises(SystemExit, match="PIPELINE_POLICY_ENFORCE"):
        rt.serve(mcp, environ=env)
    assert mcp.calls == []


def test_serve_refuses_to_start_with_enforcement_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {**REMOTE_ENV, ENFORCE_ENV: "off", ALLOWED_HOSTS_ENV: "mcp.example.com"}
    mcp = FakeMCP()
    monkeypatch.setitem(
        sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None)
    )
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: Mock(spec=OidcProvider)
    )
    with pytest.raises(SystemExit, match="PIPELINE_POLICY_ENFORCE"):
        rt.serve(mcp, environ=env)
    assert mcp.calls == []


def test_proxied_host_is_not_rejected_by_the_real_sdk() -> None:
    from mcp.server import MCPServer
    from starlette.testclient import TestClient

    app = MCPServer("hosts-test").streamable_http_app(
        host="127.0.0.1",
        transport_security=rt._transport_security(
            {ALLOWED_HOSTS_ENV: "mcp.example.com"}
        ),
    )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=body,
            headers={
                "Host": "mcp.example.com",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code != 421
