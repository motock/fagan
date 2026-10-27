"""Remote MCP transport: streamable HTTP behind OIDC bearer authentication.

The default transport is stdio, and it stays byte-identical: nothing in this
module runs unless ``PIPELINE_MCP_TRANSPORT`` names an HTTP transport. Enabling
one is a deliberate two-part act, and the second part is non-negotiable:

* the transport must be named explicitly (``streamable-http`` or ``sse``);
* the identity provider must be OIDC, because a remote MCP endpoint that
  authenticates nobody is an unauthenticated control plane over the network.
  A missing or different provider refuses to start rather than serving.

The gate is raw ASGI on purpose. ``streamable_http_app()`` returns a Starlette
app, and wrapping it here without importing Starlette keeps this module
runnable from a minimal install (stdio users never import it at all).
"""

from __future__ import annotations

import logging
from typing import Any

from app.identity import IdentityError
from app.oidc import OidcProvider

from .actor import bind_correlation_id, bind_principal, request_correlation_id

logger = logging.getLogger("pipeline.remote_transport")


def _header(scope: dict[str, Any], name: str) -> str | None:
    """Return a request header from a raw ASGI scope, case-insensitively."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


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
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI gate: verify a bearer token, bind the principal, tag the response.

    Every HTTP request to the mounted app must carry ``Authorization: Bearer
    <token>``. A rejected request gets the same 401 body the dashboard uses, so
    a client cannot tell which process it spoke to. Lifespan and websocket
    scopes pass through untouched.
    """

    def __init__(self, app: Any, provider: OidcProvider) -> None:
        self._app = app
        self._provider = provider

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        authorization = _header(scope, "authorization") or ""
        scheme, _, token = authorization.partition(" ")
        credential = token.strip() if scheme.lower() == "bearer" else None
        correlation = request_correlation_id(_header(scope, "x-correlation-id"))
        try:
            principal = self._provider.authenticate(credential)
        except IdentityError:
            logger.warning(
                "remote mcp request rejected",
                extra={"correlation_id": correlation, "path": scope.get("path", "")},
            )
            await _send_unauthorized(send, correlation)
            return

        async def _tagged(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-correlation-id", correlation.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with bind_principal(principal), bind_correlation_id(correlation):
            await self._app(scope, receive, _tagged)
