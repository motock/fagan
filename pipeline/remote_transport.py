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

Two token buckets bound the gate (``pipeline.rate_limit``): a failed-auth bucket
per client address, checked *before* token verification so a blocked client
costs no JWKS work, and a request bucket per authenticated subject. Going over
either answers ``429`` with ``Retry-After``. Both live in process memory, so a
multi-replica deployment needs a shared limiter. The JWKS refetch bound in
``app.oidc.verify_token`` stays in place: an unauthenticated endpoint that
refetches keys per request is a denial-of-service amplifier.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings

from app.identity import IdentityError
from app.oidc import IDENTITY_PROVIDER_ENV, OidcProvider, build_identity_provider

from .actor import bind_correlation_id, bind_principal, request_correlation_id
from .audit import append_host_audit
from .authorization import enforcement_enabled
from .rate_limit import (
    AUTH_FAIL_ENV,
    DEFAULT_AUTH_FAIL_PER_MIN,
    DEFAULT_REQUESTS_PER_MIN,
    REQUESTS_ENV,
    TRUSTED_PROXIES_ENV,
    RateLimiter,
    parse_count,
    parse_trusted_proxies,
)
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


def build_rate_limiter(
    *, clock: Any = None, environ: dict[str, str] | None = None
) -> RateLimiter:
    """Build the gate's limiter from the environment, or refuse to start.

    A malformed counter or proxy list raises ``ValueError`` naming the variable,
    so ``serve()`` never binds a socket with a limiter that silently fell back to
    a default. Buckets are in-process memory; a multi-replica deployment needs a
    shared limiter instead.
    """
    env = os.environ if environ is None else environ
    auth_fail = parse_count(AUTH_FAIL_ENV, DEFAULT_AUTH_FAIL_PER_MIN, env)
    requests = parse_count(REQUESTS_ENV, DEFAULT_REQUESTS_PER_MIN, env)
    trusted = parse_trusted_proxies(env.get(TRUSTED_PROXIES_ENV), env)
    return RateLimiter(auth_fail, requests, clock=clock, trusted_proxies=trusted)


def _logged_path(scope: dict[str, Any]) -> str:
    """Return a bounded, log-safe copy of the request path."""
    path = scope.get("path") or ""
    return str(path)[:_MAX_LOGGED_PATH]


async def _send_unauthorized(
    send: Any, correlation: str, retry_after: int | None = None
) -> None:
    """Send the fixed rejection body. The credential is never echoed.

    With *retry_after* set this is the gate's 429: the same JSON body and
    correlation header, plus ``Retry-After``. Neither form carries a limit
    value, a subject, a client address or a credential, so a throttled caller
    learns only that it must wait.
    """
    body = b'{"detail":"Unauthorized"}'
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-correlation-id", correlation.encode("latin-1")),
    ]
    if retry_after is None:
        # RFC 6750 3: a bearer-protected resource must challenge, and
        # MCP clients use this header to discover the authorization
        # server, so omitting it blocks a conformant client's bootstrap.
        headers.append((b"www-authenticate", b"Bearer"))
    else:
        headers.append(
            (b"retry-after", str(max(1, int(retry_after))).encode("ascii"))
        )
    headers.append((b"cache-control", b"no-store"))
    await send(
        {
            "type": "http.response.start",
            "status": 401 if retry_after is None else 429,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI gate: verify a bearer token, bind the principal, tag the response.

    Every HTTP request to the mounted app must carry exactly one
    ``Authorization: Bearer <token>`` header. A rejected request gets the same
    401 body the dashboard uses, so a client cannot tell which process it spoke
    to, plus a ``WWW-Authenticate: Bearer`` challenge. A request over either
    rate limit gets that same body with ``429`` and ``Retry-After`` instead.
    ``lifespan`` scopes pass through untouched; every other scope type is
    closed without reaching the app.
    """

    def __init__(
        self,
        app: Any,
        provider: OidcProvider,
        *,
        clock: Any = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(provider, OidcProvider):
            raise TypeError(
                "remote mcp transport requires an OidcProvider; refusing to serve "
                f"with {type(provider).__name__}"
            )
        self._app = app
        self._provider = provider
        self._limiter = build_rate_limiter(clock=clock, environ=environ)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope_type != "http":
            await self._deny_non_http(scope, send)
            return

        correlation = request_correlation_id(_header(scope, "x-correlation-id"))
        client_ip = self._limiter.client_ip(scope)
        blocked = self._limiter.peek_failed_auth(client_ip)
        if not blocked.allowed:
            await self._reject_rate_limited(
                correlation,
                blocked.retry_after,
                send,
                kind="client",
                key=client_ip,
                first=self._limiter.note_rejection(client_ip),
            )
            return
        credential, reason = self._credential(scope)
        principal = self._authenticate(credential, reason)
        if principal is None:
            self._audit(
                scope,
                correlation,
                outcome="denied",
                detail=reason or "credential rejected",
            )
            self._limiter.record_failed_auth(client_ip)
            await _send_unauthorized(send, correlation)
            return

        admission = self._limiter.check_request(principal.subject)
        if not admission.allowed:
            await self._reject_rate_limited(
                correlation,
                admission.retry_after,
                send,
                kind="subject",
                key=principal.subject,
                first=admission.audit_key is not None,
            )
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

    async def _reject_rate_limited(
        self,
        correlation: str,
        retry_after: int,
        send: Any,
        *,
        kind: str,
        key: str,
        first: bool,
    ) -> None:
        """Answer an over-limit request with the gate's 429.

        Only the first rejection of a window reaches the host audit chain, so a
        sustained flood cannot flood the chain itself.
        """
        if first:
            self._audit_rate_limited(kind, key, correlation)
        await _send_unauthorized(send, correlation, retry_after=retry_after)

    def _audit_rate_limited(self, kind: str, key: str, correlation: str) -> None:
        """Record one rate-limit rejection in the host audit chain.

        The record names the action, the bucket kind and the caller only -- a
        client address or a principal subject -- never a credential, payload,
        path or exception text.
        """
        detail: dict[str, Any] = {"bucket": kind, kind: key}
        logger.warning(
            "remote mcp rate limited",
            extra={"outcome": "rate_limited", "correlation_id": correlation, **detail},
        )
        try:
            append_host_audit(
                action="mcp:rate_limited", outcome="rate_limited", detail=detail
            )
        except OSError:
            logger.warning("host audit append failed for rate_limited")

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
ALLOWED_HOSTS_ENV = "PIPELINE_MCP_ALLOWED_HOSTS"

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


def _transport_security(env: dict[str, str]) -> TransportSecuritySettings | None:
    """Host allowlist for a proxied deployment, or ``None`` for the SDK default."""
    hosts = [
        h.strip() for h in str(env.get(ALLOWED_HOSTS_ENV, "")).split(",") if h.strip()
    ]
    if not hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]
    )


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
    if not enforcement_enabled(env):
        raise SystemExit(
            "refusing to serve MCP over HTTP with PIPELINE_POLICY_ENFORCE off"
        )
    security = _transport_security(env)

    mcp.middleware.append(remote_tool_policy)

    app = (
        mcp.streamable_http_app(host=host, transport_security=security)
        if transport == "streamable-http"
        else mcp.sse_app(host=host, transport_security=security)
    )
    gated = BearerAuthMiddleware(app, provider, environ=env)

    import uvicorn  # imported here so stdio users never need it

    logger.warning(
        "serving MCP over %s on %s:%s behind OIDC bearer authentication",
        transport,
        host,
        port,
    )
    uvicorn.run(gated, host=host, port=port, log_level="info")
