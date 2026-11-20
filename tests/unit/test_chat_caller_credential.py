"""Chat tool calls act as the signed-in caller under OIDC (ccc-01).

Under ``PIPELINE_IDENTITY_PROVIDER=oidc`` the dashboard chat (``/api/chat`` and
``/api/chat/stream``) is driven by a browser that holds only a session cookie
and an ``X-Fagan-Namespace`` header, never an API key. ChatService's internal
httpx client calls this same server's ``/api/*`` routes, so each tool call must
carry the caller's own credential or the middleware answers 401.

These tests exercise the REAL app (``app.dashboard.app`` with its real auth
middleware) through a real ``TestClient``. The internal hops are recorded by a
``TestClient`` subclass injected as ChatService's ``http_client``, so the
assertions read the headers the inner request actually sent and the status the
real middleware returned for it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth, chat, oidc_login
from app import dashboard as d
from app.identity import IdentityError, Principal, SharedSecretProvider
from app.oidc import OidcProvider

REDIRECT_URI = "https://dashboard.example/auth/callback"
ISSUER = "https://issuer.example"
GOOD_TOKEN = "good"
BAD_TOKEN = "bad"
SESSION_SUBJECT = "https://issuer.example#bob"
NAMESPACE = "default"
CHAT_PATH = "/api/chat"
STREAM_PATH = "/api/chat/stream"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


class _ScriptedDriver:
    """Emits one TOOL_CALL turn (a bare JSON object, matching the real wire
    format SYSTEM_PROMPT instructs), then a plain final reply."""

    model = "scripted"

    def __init__(self, tool_call: dict) -> None:
        self._tool_call = tool_call
        self._used = False

    def complete(self, *, prompt, system, model, cwd):
        if not self._used:
            self._used = True
            return "[TOOL_CALL] " + json.dumps(self._tool_call) + " [/TOOL_CALL]"
        return "done"


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that accepts exactly one bearer token, no network I/O."""

    def __init__(self) -> None:
        self._principal = Principal(subject="https://issuer.example#alice", auth_method="oidc")

    def authenticate(self, credential: str | None) -> Principal:
        if credential != GOOD_TOKEN:
            raise IdentityError("invalid token")
        return self._principal


class _RecordingClient(TestClient):
    """A TestClient that records each internal hop's request headers and status."""

    def __init__(self, app, hops: list) -> None:
        super().__init__(app)
        self._hops = hops

    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        self._hops.append((dict(response.request.headers), response.status_code))
        return response


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    oidc_login.reset_state()
    monkeypatch.setattr(auth, "API_KEY_PATH", tmp_path / ".dashboard_api_key")
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri=REDIRECT_URI,
            issuer=ISSUER,
        ),
    )
    yield
    oidc_login.reset_state()


@pytest.fixture
def hops(monkeypatch):
    """Route every ChatService's internal client through the recording client."""
    recorded: list = []
    original_init = chat.ChatService.__init__

    def init_with_recording_client(self, **kwargs):
        kwargs["http_client"] = _RecordingClient(d.app, recorded)
        original_init(self, **kwargs)

    monkeypatch.setattr(chat.ChatService, "__init__", init_with_recording_client)
    return recorded


def _use_tool(monkeypatch, tool_call: dict) -> None:
    driver = _ScriptedDriver(tool_call)
    monkeypatch.setattr(chat.ChatService, "_resolve_driver", lambda self: (driver, "scripted"))


def _session_cookie() -> tuple[str, str]:
    session_id = oidc_login._create_session(Principal(subject=SESSION_SUBJECT, auth_method="oidc"))
    return session_id, f"{oidc_login.SESSION_COOKIE}={session_id}"


def _list_plans_message() -> dict:
    return {"message": "what plans are there?"}


def test_session_cookie_authorises_the_list_plans_tool_call(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    _, cookie = _session_cookie()

    response = TestClient(d.app).post(CHAT_PATH, json=_list_plans_message(), headers={"Cookie": cookie, **SAME_ORIGIN})

    assert response.status_code == 200
    assert [status for _, status in hops] == [200]


def test_bearer_token_authorises_the_list_plans_tool_call(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    response = TestClient(d.app).post(
        CHAT_PATH, json=_list_plans_message(), headers={"Authorization": f"Bearer {GOOD_TOKEN}"}
    )

    assert response.status_code == 200
    assert [status for _, status in hops] == [200]


def test_session_cookie_authorises_the_list_plans_tool_call_through_the_stream(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    _, cookie = _session_cookie()

    response = TestClient(d.app).post(
        STREAM_PATH, json=_list_plans_message(), headers={"Cookie": cookie, **SAME_ORIGIN}
    )

    assert response.status_code == 200
    assert "event: tool_result" in response.text
    assert [status for _, status in hops] == [200]


def test_bearer_token_authorises_the_list_plans_tool_call_through_the_stream(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    response = TestClient(d.app).post(
        STREAM_PATH, json=_list_plans_message(), headers={"Authorization": f"Bearer {GOOD_TOKEN}"}
    )

    assert response.status_code == 200
    assert "event: tool_result" in response.text
    assert [status for _, status in hops] == [200]


def test_bearer_forwards_the_callers_namespace_header(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    TestClient(d.app).post(
        CHAT_PATH,
        json=_list_plans_message(),
        headers={"Authorization": f"Bearer {GOOD_TOKEN}", "X-Fagan-Namespace": NAMESPACE},
    )

    inner_headers, _ = hops[0]
    assert inner_headers["x-fagan-namespace"] == NAMESPACE


def test_bearer_without_a_namespace_header_forwards_no_namespace(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    TestClient(d.app).post(
        CHAT_PATH, json=_list_plans_message(), headers={"Authorization": f"Bearer {GOOD_TOKEN}"}
    )

    inner_headers, _ = hops[0]
    assert "x-fagan-namespace" not in inner_headers


def test_bearer_wins_over_a_valid_session_cookie(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    _, cookie = _session_cookie()

    TestClient(d.app).post(
        CHAT_PATH,
        json=_list_plans_message(),
        headers={"Authorization": f"Bearer {GOOD_TOKEN}", "Cookie": cookie, **SAME_ORIGIN},
    )

    inner_headers, _ = hops[0]
    assert "authorization" in inner_headers
    assert "cookie" not in inner_headers


def test_cross_site_session_cookie_is_refused_before_any_tool_call(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    _, cookie = _session_cookie()

    response = TestClient(d.app).post(
        CHAT_PATH, json=_list_plans_message(), headers={"Cookie": cookie, "Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert hops == []


def test_no_credential_is_refused_before_any_tool_call(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    response = TestClient(d.app).post(CHAT_PATH, json=_list_plans_message())

    assert response.status_code == 401
    assert hops == []


def test_rejected_bearer_does_not_downgrade_to_the_session_cookie(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    _, cookie = _session_cookie()

    response = TestClient(d.app).post(
        CHAT_PATH,
        json=_list_plans_message(),
        headers={"Authorization": f"Bearer {BAD_TOKEN}", "Cookie": cookie, **SAME_ORIGIN},
    )

    assert response.status_code == 401
    assert hops == []


def test_session_id_never_appears_in_the_chat_response(monkeypatch, hops):
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})
    session_id, cookie = _session_cookie()

    response = TestClient(d.app).post(CHAT_PATH, json=_list_plans_message(), headers={"Cookie": cookie, **SAME_ORIGIN})

    assert session_id not in response.text


def test_write_tool_call_with_session_cookie_reaches_the_route_as_the_caller(monkeypatch, hops):
    # cpa-02: pause_plan is now only proposed, so save_plan is the write the chat still sends itself.
    _use_tool(monkeypatch, {"name": "save_plan", "args": {"plan_name": "no-such-plan", "plan_json": "{\"epics\": []}"}})
    _, cookie = _session_cookie()

    TestClient(d.app).post(CHAT_PATH, json=_list_plans_message(), headers={"Cookie": cookie, **SAME_ORIGIN})

    inner_status = hops[0][1]
    assert inner_status not in (401, 403)


def test_shared_secret_chat_tool_call_uses_the_api_key_and_no_caller_credential(monkeypatch, hops):
    monkeypatch.setattr(d, "_identity", SharedSecretProvider(auth.get_or_create_api_key))
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(enabled=False, client_id="", redirect_uri="", issuer=""),
    )
    _use_tool(monkeypatch, {"name": "list_plans", "args": {}})

    response = TestClient(d.app).post(
        CHAT_PATH, json=_list_plans_message(), headers={"X-Pipeline-Api-Key": auth.get_or_create_api_key()}
    )

    assert response.status_code == 200
    inner_headers, inner_status = hops[0]
    assert inner_status == 200
    assert "authorization" not in inner_headers
    assert "cookie" not in inner_headers
