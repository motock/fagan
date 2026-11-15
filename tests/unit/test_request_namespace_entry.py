"""enter_request_namespace verifies the requested namespace, then binds it.

The context manager is the single entry point a transport uses: a verified
namespace is bound for the life of the request and restored afterwards, and a
denial raises NamespaceDenied before the request body runs.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import pipeline.namespace_access as access_module
import pipeline.server
from app import dashboard as d
from app.identity import ROLE_AUTHOR, IdentityError, Principal
from app.oidc import OidcProvider
from pipeline import advance_one
from pipeline import remote_transport as rt
from pipeline.actor import bind_namespace, current_namespace
from pipeline.namespace_access import (
    NAMESPACE_MALFORMED,
    NAMESPACE_MISSING,
    NAMESPACE_NOT_PERMITTED,
    NamespaceDenied,
    enter_request_namespace,
)
from pipeline.namespace_resolver import DefaultNamespaceResolver, NamespaceAccess
from pipeline.remote_transport import BearerAuthMiddleware
from pipeline.store_records import DEFAULT_NAMESPACE

ALICE = Principal(
    subject="alice",
    roles=frozenset({ROLE_AUTHOR}),
    correlation_id="corr-enter-1",
)


class _StubResolver:
    """Resolver double returning a fixed tuple of accesses."""

    def __init__(self, *accesses: NamespaceAccess) -> None:
        self._accesses = accesses

    def accesses(self, principal):
        return self._accesses


@pytest.fixture
def audit_calls(monkeypatch):
    """Capture host audit appends without touching the real chain."""
    calls: list[dict] = []
    monkeypatch.setattr(
        access_module, "append_host_audit", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def _use_resolver(monkeypatch, resolver) -> None:
    monkeypatch.setattr(access_module, "active_namespace_resolver", lambda: resolver)


# Positive: the unchanged path with no plugin configured.


def test_absent_header_with_default_resolver_binds_the_default_namespace():
    with enter_request_namespace(ALICE, None):
        assert current_namespace() == DEFAULT_NAMESPACE


def test_default_resolver_namespace_is_restored_after_the_request():
    with bind_namespace("outer-ns"):
        with enter_request_namespace(ALICE, None):
            pass
        assert current_namespace() == "outer-ns"


def test_default_resolver_writes_no_audit_record_on_success(audit_calls):
    with enter_request_namespace(ALICE, None):
        pass
    assert audit_calls == []


def test_listed_namespace_is_bound_for_the_request(monkeypatch):
    _use_resolver(
        monkeypatch,
        _StubResolver(
            NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
            NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
        ),
    )
    with enter_request_namespace(ALICE, "beta"):
        assert current_namespace() == "beta"


# Negative: each denial raises with its reason token and binds nothing.


def test_two_accesses_and_no_header_is_namespace_missing(monkeypatch, audit_calls):
    _use_resolver(
        monkeypatch,
        _StubResolver(
            NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
            NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
        ),
    )
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(ALICE, None):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_MISSING
    assert len(audit_calls) == 1


def test_unlisted_namespace_is_not_permitted_and_audited(monkeypatch, audit_calls):
    _use_resolver(
        monkeypatch,
        _StubResolver(NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))),
    )
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(ALICE, "gamma"):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_NOT_PERMITTED
    assert len(audit_calls) == 1
    assert audit_calls[0]["detail"]["reason"] == NAMESPACE_NOT_PERMITTED


def test_malformed_namespace_is_refused_before_the_resolver(monkeypatch):
    def _unreachable():
        pytest.fail("the resolver must not be consulted for a malformed value")

    monkeypatch.setattr(access_module, "active_namespace_resolver", _unreachable)
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(
        ALICE, "Not A Namespace!"
    ):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_MALFORMED


def test_denial_leaves_the_previously_bound_namespace_unchanged(monkeypatch):
    _use_resolver(
        monkeypatch,
        _StubResolver(NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))),
    )
    with bind_namespace("outer-ns"):
        with pytest.raises(NamespaceDenied), enter_request_namespace(ALICE, "gamma"):
            pass
        assert current_namespace() == "outer-ns"


# Entry points: the remote MCP transport, the dashboard and the advance child.
# Each test drives the real entry point, so removing the wiring fails it.

NAMESPACE_HEADER = "x-fagan-namespace"
ALICE_TOKEN = "alice-token"
ALICE_OIDC = Principal(
    subject="alice", auth_method="oidc", roles=frozenset({ROLE_AUTHOR})
)


class _StubProvider(OidcProvider):
    """Identity double: accepts Alice's token only. No settings, no network."""

    def __init__(self) -> None:
        self._settings = None

    def authenticate(self, credential: str | None) -> Principal:
        if credential != ALICE_TOKEN:
            raise IdentityError("credential rejected")
        return ALICE_OIDC


class _RecordingApp:
    """Downstream ASGI app: records the namespace it runs in, then sends 200."""

    def __init__(self) -> None:
        self.seen_namespaces: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.seen_namespaces.append(current_namespace())
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _drive_transport(
    app: _RecordingApp, *headers: tuple[bytes, bytes]
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Run one authenticated MCP request through the gate: (status, headers, body)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (b"authorization", f"Bearer {ALICE_TOKEN}".encode("latin-1")),
            *headers,
        ],
        "client": ("203.0.113.9", 4321),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    middleware = BearerAuthMiddleware(app, _StubProvider(), environ={})
    asyncio.run(middleware(scope, receive, send))
    start = messages[0]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return start["status"], dict(start["headers"]), body


def _namespace_header(value: str) -> tuple[bytes, bytes]:
    return (NAMESPACE_HEADER.encode("latin-1"), value.encode("latin-1"))


@pytest.fixture
def transport_audit(monkeypatch, audit_calls):
    """Silence the transport's own auth audit; keep the namespace denial audit."""
    monkeypatch.setattr(rt, "append_host_audit", lambda **kwargs: None)
    return audit_calls


@pytest.fixture
def two_namespace_resolver(monkeypatch):
    """A resolver where Alice reaches two namespaces, so no header is ambiguous."""
    _use_resolver(
        monkeypatch,
        _StubResolver(
            NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
            NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
        ),
    )


# Remote MCP transport.


def test_transport_without_header_on_default_resolver_runs_in_default_namespace(
    transport_audit,
):
    app = _RecordingApp()
    status, _, body = _drive_transport(app)
    assert (status, body) == (200, b"ok")
    assert app.seen_namespaces == [DEFAULT_NAMESPACE]
    assert transport_audit == []


def test_transport_restores_the_outer_namespace_after_the_request(transport_audit):
    app = _RecordingApp()
    with bind_namespace("outer-ns"):
        _drive_transport(app)
        assert current_namespace() == "outer-ns"


def test_transport_binds_a_header_namespace_the_principal_may_reach(
    two_namespace_resolver, transport_audit
):
    app = _RecordingApp()
    status, _, _ = _drive_transport(app, _namespace_header("beta"))
    assert status == 200
    assert app.seen_namespaces == ["beta"]


def test_transport_refuses_missing_header_with_two_accesses(
    two_namespace_resolver, transport_audit
):
    app = _RecordingApp()
    status, headers, body = _drive_transport(
        app, (b"x-correlation-id", b"corr-transport-1")
    )
    assert status == 403
    assert json.loads(body) == {
        "detail": NAMESPACE_MISSING,
        "correlation_id": "corr-transport-1",
    }
    assert headers[b"x-correlation-id"] == b"corr-transport-1"
    assert app.seen_namespaces == []
    assert [entry["detail"]["reason"] for entry in transport_audit] == [
        NAMESPACE_MISSING
    ]


def test_transport_refuses_an_unlisted_namespace_and_audits_it(
    two_namespace_resolver, transport_audit
):
    app = _RecordingApp()
    status, _, body = _drive_transport(app, _namespace_header("gamma"))
    assert status == 403
    assert json.loads(body)["detail"] == NAMESPACE_NOT_PERMITTED
    assert app.seen_namespaces == []
    assert len(transport_audit) == 1


def test_transport_refuses_a_malformed_header_without_echoing_it(
    transport_audit,
):
    app = _RecordingApp()
    status, _, body = _drive_transport(app, _namespace_header("Bad Name!"))
    assert status == 403
    assert json.loads(body)["detail"] == NAMESPACE_MALFORMED
    assert b"Bad Name" not in body
    assert "Bad Name" not in json.dumps(transport_audit)
    assert app.seen_namespaces == []


def test_transport_refuses_a_repeated_header_as_malformed(transport_audit):
    app = _RecordingApp()
    status, _, body = _drive_transport(
        app, _namespace_header("alpha"), _namespace_header("beta")
    )
    assert status == 403
    assert json.loads(body)["detail"] == NAMESPACE_MALFORMED
    assert app.seen_namespaces == []


# Dashboard. The API-key gate is not under test here, so it is stubbed out.


@pytest.fixture
def dashboard_client(monkeypatch, transport_audit):
    monkeypatch.setattr(d, "require_api_key", lambda *args, **kwargs: None)
    return TestClient(d.app)


def test_dashboard_without_header_on_default_resolver_is_not_refused(
    dashboard_client,
):
    response = dashboard_client.get("/api/health")
    assert response.status_code != 403


def test_dashboard_refuses_missing_header_with_two_accesses(
    dashboard_client, two_namespace_resolver
):
    response = dashboard_client.get(
        "/api/health", headers={"x-correlation-id": "corr-dash-1"}
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": NAMESPACE_MISSING,
        "correlation_id": "corr-dash-1",
    }
    assert response.headers["x-correlation-id"] == "corr-dash-1"


def test_dashboard_refuses_an_unlisted_header_namespace(
    dashboard_client, two_namespace_resolver
):
    response = dashboard_client.get(
        "/api/health", headers={NAMESPACE_HEADER: "gamma"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_NOT_PERMITTED


def test_dashboard_refuses_a_malformed_header_without_echoing_it(dashboard_client):
    response = dashboard_client.get(
        "/api/health", headers={NAMESPACE_HEADER: "Bad Name!"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_MALFORMED
    assert "Bad Name" not in response.text


def test_dashboard_admits_a_header_namespace_the_principal_may_reach(
    dashboard_client, two_namespace_resolver
):
    response = dashboard_client.get("/api/health", headers={NAMESPACE_HEADER: "alpha"})
    assert response.status_code != 403


# Advance child: binds the plan record's namespace, else the default.


@pytest.fixture
def advance_stub(monkeypatch):
    seen: list[str] = []

    def stub_advance(plan_name: str) -> dict[str, bool]:
        seen.append(current_namespace())
        return {"ok": True}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", stub_advance)
    monkeypatch.setattr(advance_one, "configure_child", lambda: None)
    return seen


def _set_plan_record(monkeypatch, manifest):
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "_plan_manifest", lambda name: manifest)


def test_advance_binds_the_default_namespace_when_the_plan_has_none(
    monkeypatch, advance_stub, tmp_path
):
    _set_plan_record(monkeypatch, {"stories": []})
    assert advance_one.main(["plan-a", str(tmp_path / "out.json")]) == 0
    assert advance_stub == [DEFAULT_NAMESPACE]


def test_advance_binds_the_plan_record_namespace(monkeypatch, advance_stub, tmp_path):
    _set_plan_record(monkeypatch, {"namespace": "alpha", "stories": []})
    assert advance_one.main(["plan-a", str(tmp_path / "out.json")]) == 0
    assert advance_stub == ["alpha"]


def test_advance_refuses_an_invalid_plan_namespace_and_reports_class_only(
    monkeypatch, advance_stub, tmp_path
):
    _set_plan_record(monkeypatch, {"namespace": "Bad Name!", "stories": []})
    result_path = tmp_path / "out.json"
    assert advance_one.main(["plan-a", str(result_path)]) == 1
    assert advance_stub == []
    payload = json.loads(result_path.read_text())
    assert payload == {
        "ok": False,
        "error": "advance raised InvalidRecordKeyError",
    }


# The default resolver accepts a principal whose roles arrive as a tuple.
# Principal does not coerce roles, and decision callers build them as tuples.


def test_default_resolver_accepts_a_principal_with_tuple_roles():
    principal = Principal(subject="carol", roles=(ROLE_AUTHOR,))
    (access,) = DefaultNamespaceResolver().accesses(principal)
    assert access.namespace == DEFAULT_NAMESPACE
    assert access.roles == frozenset({ROLE_AUTHOR})


def test_default_resolver_gives_tuple_roles_as_a_frozenset():
    principal = Principal(subject="carol", roles=(ROLE_AUTHOR,))
    (access,) = DefaultNamespaceResolver().accesses(principal)
    assert isinstance(access.roles, frozenset)
