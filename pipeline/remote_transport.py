"""Remote MCP transport: streamable HTTP behind OIDC bearer authentication.

The default transport is stdio, and it stays byte-identical: nothing in this
module runs unless ``PIPELINE_MCP_TRANSPORT`` names an HTTP transport. Enabling
one is a deliberate two-part act, and the second part is non-negotiable:

* the transport must be named explicitly (``streamable-http`` or ``sse``);
* the identity provider must be OIDC, because a remote MCP endpoint that
  authenticates nobody is an unauthenticated control plane over the network.
  A missing or different provider refuses to start rather than serving.
  ``BearerAuthMiddleware`` enforces this at construction time: anything that is
  not an ``OidcProvider`` raises ``TypeError``, so the default
  ``SharedSecretProvider`` cannot be wired in by accident.

The gate is raw ASGI on purpose. ``streamable_http_app()`` returns a Starlette
app, and wrapping it here without importing Starlette keeps this module
runnable from a minimal install (stdio users never import it at all).

Deny by default: only ``lifespan`` scopes bypass authentication (uvicorn sends
one during startup, and 401-ing it would prevent the app from booting). An
``http`` scope must carry a bearer token; any other scope type, including
``websocket``, is closed without reaching the wrapped app -- a WebSocket route
on the mounted app would otherwise be reachable unauthenticated.

Rate limiting is not implemented here. The gate is the mount point for it, and
``app.oidc.verify_token`` bounds JWKS refetches, so a future implementer should
keep that bound in place rather than dropping it: an unauthenticated endpoint
that refetches keys per request is a denial-of-service amplifier.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.identity import IdentityError
from app.oidc import IDENTITY_PROVIDER_ENV, OidcProvider, build_identity_provider

from .actor import bind_correlation_id, bind_principal, request_correlation_id
from .audit import append_host_audit
from .remote_tool_policy import remote_tool_policy

logger = logging.getLogger("pipeline.remote_transport")

#: Longest request path copied into a log record; paths are attacker-controlled.
_MAX_LOGGED_PATH = 256


def _header_values(scope: dict[str, Any], name: str) -> list[str]:
    """Return every value of header *name* in a raw ASGI scope, case-insensitively."""
    wanted = name.lower().encode("latin-1")
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers") or ()
        if key.lower() == wanted
    ]


def _header(scope: dict[str, Any], name: str) -> str | None:
    """Return a request header from a raw ASGI scope, case-insensitively."""
    values = _header_values(scope, name)
    return values[0] if values else None


def _client_ip(scope: dict[str, Any]) -> str:
    """Return the peer address from the ASGI scope, or an empty string."""
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


def _logged_path(scope: dict[str, Any]) -> str:
    """Return a bounded, log-safe copy of the request path."""
    path = scope.get("path") or ""
    return str(path)[:_MAX_LOGGED_PATH]


async def _send_unauthorized(send: Any, correlation: str) -> None:
    """Send the fixed 401 body. The credential is never echoed."""
    body = b'{"detail":"Unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-correlation-id", correlation.encode("latin-1")),
                # RFC 6750 3: a bearer-protected resource must challenge, and
                # MCP clients use this header to discover the authorization
                # server, so omitting it blocks a conformant client's bootstrap.
                (b"www-authenticate", b"Bearer"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI gate: verify a bearer token, bind the principal, tag the response.

    Every HTTP request to the mounted app must carry exactly one
    ``Authorization: Bearer <token>`` header. A rejected request gets the same
    401 body the dashboard uses, so a client cannot tell which process it spoke
    to, plus a ``WWW-Authenticate: Bearer`` challenge. ``lifespan`` scopes pass
    through untouched; every other scope type is closed without reaching the app.
    """

    def __init__(self, app: Any, provider: OidcProvider) -> None:
        if not isinstance(provider, OidcProvider):
            raise TypeError(
                "remote mcp transport requires an OidcProvider; refusing to serve "
                f"with {type(provider).__name__}"
            )
        self._app = app
        self._provider = provider

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope_type != "http":
            await self._deny_non_http(scope, send)
            return

        correlation = request_correlation_id(_header(scope, "x-correlation-id"))
        credential, reason = self._credential(scope)
        principal = self._authenticate(credential, reason)
        if principal is None:
            self._audit(
                scope,
                correlation,
                outcome="denied",
                detail=reason or "credential rejected",
            )
            await _send_unauthorized(send, correlation)
            return

        self._audit(scope, correlation, outcome="authenticated", subject=principal.subject)

        async def _tagged(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-correlation-id", correlation.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with bind_principal(principal), bind_correlation_id(correlation):
            await self._app(scope, receive, _tagged)

    def _authenticate(self, credential: str | None, reason: str | None) -> Any:
        """Return the authenticated principal, or ``None`` when the request fails."""
        if reason is not None:
            return None
        try:
            return self._provider.authenticate(credential)
        except IdentityError:
            return None

    def _credential(self, scope: dict[str, Any]) -> tuple[str | None, str | None]:
        """Return ``(credential, deny_reason)`` for an HTTP scope.

        A repeated ``Authorization`` header is denied rather than resolved to
        one value: a reverse proxy that appends instead of replacing lets the
        proxy validate one token while this gate validates another.
        """
        values = _header_values(scope, "authorization")
        if len(values) > 1:
            return None, "duplicate authorization header"
        scheme, _, token = (values[0] if values else "").partition(" ")
        if scheme.lower() != "bearer":
            return None, "not a bearer credential"
        return token.strip(), None

    def _audit(
        self,
        scope: dict[str, Any],
        correlation: str,
        *,
        outcome: str,
        subject: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Record one authentication attempt: outcome, caller, correlation, path.

        Rejections log at ``WARNING`` and successes at ``INFO``. The credential
        never appears here, and neither does the provider's exception text, which
        could quote the token; ``detail`` is always a fixed reason string.
        """
        extra: dict[str, Any] = {
            "outcome": outcome,
            "correlation_id": correlation,
            "client_ip": _client_ip(scope),
            "path": _logged_path(scope),
        }
        if subject is not None:
            extra["subject"] = subject
        if detail is not None:
            extra["detail"] = detail
        level = logging.INFO if outcome == "authenticated" else logging.WARNING
        logger.log(level, "remote mcp authentication", extra=extra)
        try:
            append_host_audit(action="auth.remote_mcp", outcome=outcome, detail=extra)
        except OSError:
            logger.warning("host audit append failed for %s", outcome)

    async def _deny_non_http(self, scope: dict[str, Any], send: Any) -> None:
        """Close a non-HTTP, non-lifespan scope without reaching the app."""
        correlation = request_correlation_id(_header(scope, "x-correlation-id"))
        self._audit(scope, correlation, outcome="denied_scope")
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_unauthorized(send, correlation)


TRANSPORT_ENV = "PIPELINE_MCP_TRANSPORT"
HOST_ENV = "PIPELINE_MCP_HOST"
PORT_ENV = "PIPELINE_MCP_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: Transports this module will serve. ``stdio`` is handled by the caller and is
#: deliberately absent here: reaching this module at all means "remote".
HTTP_TRANSPORTS = ("streamable-http", "sse")


def _settings_from_env(env: dict[str, str]) -> tuple[str, str, int]:
    """Return ``(transport, host, port)``, rejecting anything unusable."""
    transport = str(env.get(TRANSPORT_ENV, "stdio")).strip()
    if transport not in HTTP_TRANSPORTS:
        raise SystemExit(
            f"{TRANSPORT_ENV} must be one of {list(HTTP_TRANSPORTS)}; got {transport!r}"
        )
    provider = str(env.get(IDENTITY_PROVIDER_ENV, "")).strip()
    if provider != "oidc":
        raise SystemExit(
            "refusing to serve MCP over HTTP without OIDC: set "
            f"{IDENTITY_PROVIDER_ENV}=oidc and the PIPELINE_OIDC_* variables, "
            "or keep the stdio transport"
        )
    host = str(env.get(HOST_ENV, DEFAULT_HOST)).strip() or DEFAULT_HOST
    raw_port = str(env.get(PORT_ENV, DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit(f"{PORT_ENV} must be an integer; got {raw_port!r}") from None
    if not 0 < port < 65536:
        raise SystemExit(f"{PORT_ENV} must be between 1 and 65535; got {port}")
    return transport, host, port


def serve(mcp: Any, environ: dict[str, str] | None = None) -> None:
    """Serve ``mcp`` over the configured HTTP transport, or refuse to start.

    Guards run before any socket is opened: an unknown transport, or a
    transport without OIDC, raises ``SystemExit`` so a misconfiguration stops
    the process instead of exposing an unauthenticated endpoint.
    """
    env = os.environ if environ is None else environ
    transport, host, port = _settings_from_env(env)
    provider = build_identity_provider(lambda: "", env)
    if not isinstance(provider, OidcProvider):
        raise SystemExit("refusing to serve MCP over HTTP without an OIDC provider")

    mcp.middleware.append(remote_tool_policy)

    app = (
        mcp.streamable_http_app(host=host)
        if transport == "streamable-http"
        else mcp.sse_app(host=host)
    )
    gated = BearerAuthMiddleware(app, provider)

    import uvicorn  # imported here so stdio users never need it

    logger.warning(
        "serving MCP over %s on %s:%s behind OIDC bearer authentication",
        transport,
        host,
        port,
    )
    uvicorn.run(gated, host=host, port=port, log_level="info")
