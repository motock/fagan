"""RFC 9728 protected-resource metadata on the remote MCP transport (SA-14).

The middleware is driven directly with a stub ASGI app and a stub provider: no
server, no socket, no Starlette. Each test asserts one property of the
metadata endpoint or of the challenge that points a client at it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.identity import IdentityError, Principal
from app.oidc import OIDC_ISSUER_ENV, OidcProvider
from pipeline import remote_transport as rt
from pipeline.rate_limit import DEFAULT_AUTH_FAIL_PER_MIN
from pipeline.remote_transport import BearerAuthMiddleware

RESOURCE_URL_ENV = "PIPELINE_MCP_RESOURCE_URL"
RESOURCE_URL = "https://pipeline.example.com/mcp"
ISSUER = "https://auth.example.com"
METADATA_PATH = "/.well-known/oauth-protected-resource"
#: What a client must be told to fetch: the origin of the resource URL, the
#: well-known path, and the resource URL's own path as the RFC 9728 3.1 suffix.
METADATA_URL = f"https://pipeline.example.com{METADATA_PATH}/mcp"

GOOD_TOKEN = "good-token"
BAD_TOKEN = "s3cr3t-token"

_PRINCIPALS = {
    GOOD_TOKEN: Principal(subject="alice", auth_method="oidc"),
}


class _StubProvider(OidcProvider):
    """Identity provider double: accepts one token, rejects the rest.

    Subclasses ``OidcProvider`` because the gate refuses every other provider
    type at construction; ``authenticate`` is overridden so no settings or
    network access are needed.
    """

    def __init__(self) -> None:
        self._settings = None

    def authenticate(self, credential: str | None) -> Principal:
        principal = _PRINCIPALS.get(credential or "")
        if principal is None:
            raise IdentityError("credential rejected")
        return principal


class _RecordingApp:
    """Downstream ASGI app: records that it was called, then sends 200."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.seen.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _enabled_env() -> dict[str, str]:
    """Environ with metadata enabled: resource URL plus authorization server."""
    return {RESOURCE_URL_ENV: RESOURCE_URL, OIDC_ISSUER_ENV: ISSUER}


def _http_scope(
    method: str = "GET",
    path: str = METADATA_PATH,
    *headers: tuple[bytes, bytes],
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": list(headers),
        "client": ("203.0.113.9", 4321),
    }


def _bearer(token: str) -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {token}".encode("latin-1"))


def _drive(
    middleware: BearerAuthMiddleware, scope: dict[str, Any]
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Run one request through the gate and return ``(status, headers, body)``."""
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))

    status = 0
    headers: dict[bytes, bytes] = {}
    body = b""
    for message in messages:
        if message["type"] == "http.response.start":
            status = int(message["status"])
            headers = {k.lower(): v for k, v in message.get("headers") or ()}
        elif message["type"] == "http.response.body":
            body += message.get("body") or b""
    return status, headers, body


def _middleware(
    app: _RecordingApp, environ: dict[str, str] | None = None
) -> BearerAuthMiddleware:
    return BearerAuthMiddleware(app, _StubProvider(), environ=environ)


# --------------------------------------------------------------------------
# Metadata endpoint: public, exact document, no downstream call
# --------------------------------------------------------------------------


def test_metadata_get_is_public_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(rt, "append_host_audit", lambda **kw: audited.append(kw))
    app = _RecordingApp()
    gate = _middleware(app, _enabled_env())

    status, headers, body = _drive(gate, _http_scope("GET", METADATA_PATH))

    assert status == 200
    assert headers[b"content-type"].startswith(b"application/json")
    assert json.loads(body) == {
        "resource": RESOURCE_URL,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid"],
    }
    assert app.seen == [], "metadata must never reach the mounted app"
    assert audited == [], "metadata must not write an auth audit record"


def test_metadata_get_accepts_the_resource_path_suffix() -> None:
    app = _RecordingApp()
    gate = _middleware(app, _enabled_env())

    status, _, body = _drive(gate, _http_scope("GET", f"{METADATA_PATH}/mcp"))

    assert status == 200
    assert json.loads(body)["resource"] == RESOURCE_URL
    assert app.seen == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_metadata_path_rejects_other_methods(method: str) -> None:
    app = _RecordingApp()
    gate = _middleware(app, _enabled_env())

    status, headers, _ = _drive(gate, _http_scope(method, METADATA_PATH))

    assert status == 405
    assert headers[b"allow"] == b"GET"
    assert app.seen == []


# --------------------------------------------------------------------------
# The 401 challenge points at the metadata document
# --------------------------------------------------------------------------


def test_unauthenticated_request_advertises_the_metadata_document() -> None:
    app = _RecordingApp()
    gate = _middleware(app, _enabled_env())

    status, headers, _ = _drive(gate, _http_scope("POST", "/mcp"))

    assert status == 401
    challenge = headers[b"www-authenticate"].decode("latin-1")
    assert f'resource_metadata="{METADATA_URL}"' in challenge
    assert app.seen == []


def test_disabled_metadata_keeps_the_plain_challenge() -> None:
    app = _RecordingApp()
    gate = _middleware(app, {})

    status, headers, _ = _drive(gate, _http_scope("GET", METADATA_PATH))

    assert status == 401
    assert headers[b"www-authenticate"] == b"Bearer"
    assert app.seen == [], "the well-known path is not public when disabled"


# --------------------------------------------------------------------------
# serve()-time validation of PIPELINE_MCP_RESOURCE_URL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "http://pipeline.example.com/mcp",
        "https://pipeline.example.com/mcp?x=1",
        "https://pipeline.example.com/mcp#frag",
        "https://user@example.com/mcp",
        "/mcp",
    ],
)
def test_invalid_resource_url_refuses_to_start(value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        rt.parse_resource_url({RESOURCE_URL_ENV: value})
    assert RESOURCE_URL_ENV in str(excinfo.value)


def test_unset_resource_url_disables_metadata() -> None:
    assert rt.parse_resource_url({}) is None


def test_valid_resource_url_is_returned_trimmed() -> None:
    assert rt.parse_resource_url({RESOURCE_URL_ENV: f"  {RESOURCE_URL}  "}) == RESOURCE_URL


# --------------------------------------------------------------------------
# Metadata requests are not authentication failures
# --------------------------------------------------------------------------


def test_metadata_requests_do_not_trip_the_failed_auth_limiter() -> None:
    app = _RecordingApp()
    gate = _middleware(app, _enabled_env())

    for _ in range(DEFAULT_AUTH_FAIL_PER_MIN + 5):
        status, _, _ = _drive(gate, _http_scope("GET", METADATA_PATH))
        assert status == 200

    # One bad credential after many metadata reads still gets the challenge,
    # not a 429: the well-known path never counted as a failed authentication.
    status, headers, _ = _drive(gate, _http_scope("POST", "/mcp", _bearer(BAD_TOKEN)))
    assert status == 401
    assert b"www-authenticate" in headers
