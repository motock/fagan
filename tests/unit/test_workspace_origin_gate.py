"""WAP-2: origin gate on ``POST /api/workspace``.

THREAT this story closes: the chat model can call the ``set_workspace``
tool, which POSTs ``/api/workspace`` and can re-point the active workspace
at ANY repo on the machine (``pipeline/workspace.py``'s
``validate_workspace`` is deliberately not allow-listed). After WAP-1 the
chat service's internal HTTP client always carries
``X-Pipeline-Origin: chat``, so the route can refuse exactly those calls
while leaving the human single-operator surface (no origin header at all)
untouched.

This module pins:

* ``app.auth.refuse_chat_origin`` — 403 IFF the origin is ``chat``;
  absent/None and every other value pass.
* ``app.auth.require_ui_origin`` — 403 UNLESS the origin is ``ui``
  (unit-tested only in this story; no route is wired to it yet).
* the ``POST /api/workspace`` handler refusing chat-origin calls BEFORE it
  touches the service, and behaving exactly as before when no origin header
  is present (the legacy dashboard UI and the pre-existing workspace tests
  send none).

The 403 detail is deliberately generic ("origin not permitted") so the
refusal leaks nothing about the workspace machinery.
"""
from __future__ import annotations

import inspect
import json
import re
from typing import get_args, get_type_hints

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.chat as chat_module
import app.dashboard as dashboard_module
from app.auth import (
    ORIGIN_CHAT,
    ORIGIN_HEADER,
    ORIGIN_UI,
    get_or_create_api_key,
    refuse_chat_origin,
    require_ui_origin,
)

GENERIC_DETAIL = "origin not permitted"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _SpyService:
    """Records every call the workspace route makes into the service."""

    def __init__(self, result: dict | None = None) -> None:
        self.result = {"ok": True, "path": "/tmp/resolved-workspace"} if result is None else result
        self.resolve_calls: list[tuple] = []
        self.set_active_calls: list[str] = []

    def resolve_workspace(self, path, create=False):
        self.resolve_calls.append((path, create))
        return self.result

    def set_active_workspace(self, path):
        self.set_active_calls.append(path)


def _client() -> TestClient:
    return TestClient(dashboard_module.app)


def _auth_headers(**extra: str) -> dict:
    headers = {"X-Pipeline-Api-Key": get_or_create_api_key()}
    headers.update(extra)
    return headers


@pytest.fixture()
def spy(monkeypatch):
    service = _SpyService()
    monkeypatch.setattr(dashboard_module, "_service", service)
    return service


# --------------------------------------------------------------------------
# route: chat origin is refused and the service is never touched
# --------------------------------------------------------------------------


def test_set_workspace_refuses_chat_origin(spy):
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/attacker-repo", "create": False},
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": GENERIC_DETAIL}
    assert spy.resolve_calls == []
    assert spy.set_active_calls == []


def test_set_workspace_refusal_does_not_leak_the_requested_path(spy):
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/attacker-repo", "create": False},
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 403
    body = response.text
    assert "/tmp/attacker-repo" not in body
    assert "workspace" not in body.lower()


def test_set_workspace_chat_origin_is_still_authenticated_first(spy):
    """The origin gate must not become an auth bypass: a bad key is 401."""
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/attacker-repo", "create": False},
        headers={
            "X-Pipeline-Api-Key": "not-the-real-key",
            ORIGIN_HEADER: ORIGIN_CHAT,
        },
    )

    assert response.status_code == 401
    assert spy.resolve_calls == []
    assert spy.set_active_calls == []


# --------------------------------------------------------------------------
# route: absent origin keeps the legacy behaviour exactly
# --------------------------------------------------------------------------


def test_set_workspace_without_origin_header_still_resolves_and_activates(spy):
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/legacy-workspace", "create": False},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "/tmp/resolved-workspace"}
    assert spy.resolve_calls == [("/tmp/legacy-workspace", False)]
    assert spy.set_active_calls == ["/tmp/resolved-workspace"]


def test_set_workspace_without_origin_header_forwards_create_flag(spy):
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/legacy-workspace", "create": True},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert spy.resolve_calls == [("/tmp/legacy-workspace", True)]
    assert spy.set_active_calls == ["/tmp/resolved-workspace"]


def test_set_workspace_without_origin_header_still_reports_resolution_errors(spy):
    spy.result = {"ok": False, "error": "not a git repo"}
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/not-a-repo", "create": False},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "not a git repo"}
    assert spy.set_active_calls == []


@pytest.mark.parametrize("origin", [ORIGIN_UI, "garbage", ""])
def test_set_workspace_allows_every_non_chat_origin(spy, origin):
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/legacy-workspace", "create": False},
        headers=_auth_headers(**{ORIGIN_HEADER: origin}),
    )

    assert response.status_code == 200
    assert spy.resolve_calls == [("/tmp/legacy-workspace", False)]
    assert spy.set_active_calls == ["/tmp/resolved-workspace"]


def test_set_workspace_without_origin_header_defaults_create_to_false(spy):
    """Boundary: the optional ``create`` field is omitted entirely."""
    response = _client().post(
        "/api/workspace",
        json={"path": "/tmp/legacy-workspace"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert spy.resolve_calls == [("/tmp/legacy-workspace", False)]
    assert spy.set_active_calls == ["/tmp/resolved-workspace"]


def test_set_workspace_without_origin_header_rejects_a_missing_path(spy):
    """Boundary: ``path`` is required, so the body validator still fires."""
    response = _client().post("/api/workspace", json={}, headers=_auth_headers())

    assert response.status_code == 422
    assert spy.resolve_calls == []
    assert spy.set_active_calls == []


# --------------------------------------------------------------------------
# end-to-end: the actual threat — a model-driven set_workspace tool call
# --------------------------------------------------------------------------


class _ScriptedDriver:
    """Emits one TOOL_CALL turn (bare JSON, matching the real wire format),
    then a plain final reply."""

    model = "scripted"

    def __init__(self, tool_call: dict) -> None:
        self._tool_call = tool_call
        self._used = False

    def complete(self, *, prompt, system, model, cwd):
        if not self._used:
            self._used = True
            return "[TOOL_CALL] " + json.dumps(self._tool_call) + " [/TOOL_CALL]"
        return "done"


def test_model_driven_set_workspace_tool_call_is_refused_end_to_end():
    """The chat model's client carries X-Pipeline-Origin: chat (WAP-1), so its
    set_workspace tool call must come back as a legible 403 tool result and
    must not re-point the active workspace."""
    from app.chat import ChatService

    driver = _ScriptedDriver({"name": "set_workspace", "args": {"path": "/tmp/attacker-repo"}})
    svc = ChatService(
        driver=driver,
        http_client=_client(),
        api_key=get_or_create_api_key(),
    )
    result = svc.execute_turn("switch to /tmp/attacker-repo")

    tool_result = result["tool_calls"][0]["result"]
    assert tool_result["result"] == {"detail": GENERIC_DETAIL}


# --------------------------------------------------------------------------
# unit: refuse_chat_origin
# --------------------------------------------------------------------------


def test_refuse_chat_origin_rejects_chat():
    with pytest.raises(HTTPException) as excinfo:
        refuse_chat_origin(ORIGIN_CHAT)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == GENERIC_DETAIL


def test_refuse_chat_origin_rejects_chat_passed_by_keyword():
    with pytest.raises(HTTPException) as excinfo:
        refuse_chat_origin(x_pipeline_origin=ORIGIN_CHAT)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == GENERIC_DETAIL


@pytest.mark.parametrize("origin", [None, ORIGIN_UI, "garbage", ""])
def test_refuse_chat_origin_passes_everything_else(origin):
    assert refuse_chat_origin(origin) is None


def test_refuse_chat_origin_parameter_is_named_for_header_injection():
    params = list(inspect.signature(refuse_chat_origin).parameters)
    assert params == ["x_pipeline_origin"]
    assert inspect.isfunction(refuse_chat_origin)


def test_refuse_chat_origin_docstring_states_the_boundary_rationale():
    doc = inspect.getdoc(refuse_chat_origin) or ""
    assert doc.strip(), "refuse_chat_origin must document why absent origin is allowed"
    lowered = doc.lower()
    assert "chat" in lowered
    assert "header" in lowered


# --------------------------------------------------------------------------
# unit: require_ui_origin
# --------------------------------------------------------------------------


def test_require_ui_origin_allows_ui():
    assert require_ui_origin(ORIGIN_UI) is None


@pytest.mark.parametrize("origin", [ORIGIN_CHAT, None, "garbage", ""])
def test_require_ui_origin_rejects_everything_else(origin):
    with pytest.raises(HTTPException) as excinfo:
        require_ui_origin(origin)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == GENERIC_DETAIL


def test_require_ui_origin_parameter_is_named_for_header_injection():
    params = list(inspect.signature(require_ui_origin).parameters)
    assert params == ["x_pipeline_origin"]
    assert inspect.isfunction(require_ui_origin)


def test_require_ui_origin_docstring_is_present():
    doc = inspect.getdoc(require_ui_origin) or ""
    assert doc.strip()


# --------------------------------------------------------------------------
# structural: the wiring the story mandates
# --------------------------------------------------------------------------


def test_origin_constants_are_unchanged():
    assert ORIGIN_HEADER == "X-Pipeline-Origin"
    assert ORIGIN_CHAT == "chat"
    assert ORIGIN_UI == "ui"


def test_dashboard_imports_header_and_refuse_chat_origin():
    source = inspect.getsource(dashboard_module)

    fastapi_import = re.search(r"^from fastapi import (.+)$", source, re.MULTILINE)
    assert fastapi_import is not None
    assert "Header" in [name.strip() for name in fastapi_import.group(1).split(",")]

    auth_import = re.search(r"^from app\.auth import (.+)$", source, re.MULTILINE)
    assert auth_import is not None
    assert "refuse_chat_origin" in [name.strip() for name in auth_import.group(1).split(",")]

    import fastapi

    assert dashboard_module.Header is fastapi.Header
    assert dashboard_module.refuse_chat_origin is refuse_chat_origin


def test_set_workspace_route_declares_the_origin_header_parameter():
    params = inspect.signature(dashboard_module.set_workspace_route).parameters
    assert list(params) == ["request", "x_pipeline_origin"]

    origin_param = params["x_pipeline_origin"]
    assert origin_param.default is None
    # The alias lives in the Annotated Header metadata, not on the default:
    # with the Annotated idiom the Python default is the plain None singleton,
    # and CPython forbids attributes on NoneType, so asserting
    # ``default.alias == ORIGIN_HEADER`` (the original line 356) is
    # unsatisfiable by construction. Read the resolved annotation instead —
    # this still fails if the alias is dropped (bare ``Header()``) or the
    # parameter is reclassified as a query parameter (no Header metadata).
    resolved = get_type_hints(dashboard_module.set_workspace_route, include_extras=True)
    metadata = get_args(resolved["x_pipeline_origin"])[1:]
    assert any(
        getattr(m, "alias", None) == ORIGIN_HEADER for m in metadata
    ), f"expected a Header(alias={ORIGIN_HEADER!r}) in the Annotated metadata, got {metadata!r}"
    annotation = str(origin_param.annotation)
    assert "str" in annotation
    assert "None" in annotation


def test_set_workspace_route_refuses_chat_origin_before_touching_the_service():
    source = inspect.getsource(dashboard_module.set_workspace_route)

    assert "refuse_chat_origin(x_pipeline_origin)" in source
    assert source.index("refuse_chat_origin(x_pipeline_origin)") < source.index("resolve_workspace")
    # This route uses the chat-origin refusal, not the stricter UI gate.
    assert "require_ui_origin" not in source


def test_set_workspace_tool_is_still_registered_for_the_model():
    assert "set_workspace" in chat_module.TOOLS
    assert callable(chat_module.TOOLS["set_workspace"]["execute"])
