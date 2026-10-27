"""The bearer-auth gate on the remote MCP transport (RMCP-01).

The middleware is driven directly, with a fake ASGI app and a fake ``send``:
no server, no socket, no Starlette. Each test asserts one property of the gate.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pipeline.remote_transport import BearerAuthMiddleware

from app.identity import IdentityError, Principal
from pipeline.actor import actor_id

#: The shape ``pipeline.actor.request_correlation_id`` accepts and mints.
CORRELATION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: The credential a rejected request presents; it must never surface.
TOKEN = "s3cr3t-token"

_PRINCIPALS = {
    "alice-token": Principal(subject="alice", auth_method="oidc"),
    "bob-token": Principal(subject="bob", auth_method="oidc"),
}


class _StubProvider:
    """Identity provider that accepts two known tokens and rejects the rest."""

    def authenticate(self, credential: str | None) -> Principal:
        principal = _PRINCIPALS.get(credential or "")
        if principal is None:
            raise IdentityError("credential rejected")
        return principal


class _RecordingApp:
    """Downstream ASGI app: records the scope and the actor, then sends 200."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.seen.append(
            {"type": scope.get("type"), "actor": actor_id(), "scope": scope}
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(*headers: tuple[bytes, bytes]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": list(headers),
    }


def _lifespan_scope() -> dict[str, Any]:
    return {"type": "lifespan", "headers": []}


def _auth(value: str) -> tuple[bytes, bytes]:
    return (b"authorization", value.encode("latin-1"))


def _correlation(value: str) -> tuple[bytes, bytes]:
    return (b"x-correlation-id", value.encode("latin-1"))


async def _drive(
    middleware: BearerAuthMiddleware, scope: dict[str, Any], sent: list[dict[str, Any]]
) -> str:
    """Run one request and return ``actor_id()`` sampled right after it returns."""

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware(scope, receive, send)
    return actor_id()


def _call(scope: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Drive the gate over *scope*; return (what the app saw, sent, actor after)."""
    app = _RecordingApp()
    sent: list[dict[str, Any]] = []
    after = asyncio.run(_drive(BearerAuthMiddleware(app, _StubProvider()), scope, sent))
    return app.seen, sent, after


def _status(sent: list[dict[str, Any]]) -> int:
    return sent[0]["status"]


def _body(sent: list[dict[str, Any]]) -> bytes:
    return b"".join(m["body"] for m in sent if m["type"] == "http.response.body")


def _header(sent: list[dict[str, Any]], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in sent[0].get("headers") or ():
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def test_valid_bearer_token_reaches_app_as_the_token_subject() -> None:
    seen, sent, _after = _call(_http_scope(_auth("Bearer alice-token")))
    assert (seen[0]["actor"], _status(sent)) == ("alice", 200)


def test_missing_authorization_header_gets_the_fixed_401() -> None:
    _seen, sent, _after = _call(_http_scope())
    assert (_status(sent), _body(sent)) == (401, b'{"detail":"Unauthorized"}')


def test_non_bearer_scheme_is_rejected() -> None:
    _seen, sent, _after = _call(_http_scope(_auth("Token xyz")))
    assert _status(sent) == 401


def test_bearer_without_a_token_is_rejected() -> None:
    _seen, sent, _after = _call(_http_scope(_auth("Bearer")))
    assert _status(sent) == 401


def test_provider_rejected_token_gets_401() -> None:
    _seen, sent, _after = _call(_http_scope(_auth(f"Bearer {TOKEN}")))
    assert _status(sent) == 401


def test_401_carries_a_well_formed_correlation_id() -> None:
    _seen, sent, _after = _call(_http_scope())
    assert CORRELATION_RE.fullmatch(_header(sent, "x-correlation-id") or "") is not None


def test_inbound_correlation_id_is_echoed_on_the_401() -> None:
    _seen, sent, _after = _call(_http_scope(_correlation("abc12345-6789")))
    assert _header(sent, "x-correlation-id") == "abc12345-6789"


def test_successful_response_carries_a_correlation_id() -> None:
    _seen, sent, _after = _call(_http_scope(_auth("Bearer bob-token")))
    assert CORRELATION_RE.fullmatch(_header(sent, "x-correlation-id") or "") is not None


def test_rejection_body_does_not_echo_the_credential() -> None:
    _seen, sent, _after = _call(_http_scope(_auth(f"Bearer {TOKEN}")))
    assert TOKEN.encode() not in _body(sent)


def test_lifespan_scope_passes_through_untouched() -> None:
    seen, sent, _after = _call(_lifespan_scope())
    assert (_status(sent) != 401, seen[0]["type"]) == (True, "lifespan")


def test_principal_binding_does_not_leak_past_the_request() -> None:
    _seen, _sent, after = _call(_http_scope(_auth("Bearer alice-token")))
    assert after == "local-operator"


def test_rejection_log_does_not_contain_the_credential(
    caplog: Any,
) -> None:
    caplog.set_level(logging.WARNING, logger="pipeline.remote_transport")
    _call(_http_scope(_auth(f"Bearer {TOKEN}")))
    leaked = [
        record.getMessage() + repr(record.__dict__)
        for record in caplog.records
        if TOKEN in record.getMessage() or TOKEN in repr(record.__dict__)
    ]
    assert leaked == []
