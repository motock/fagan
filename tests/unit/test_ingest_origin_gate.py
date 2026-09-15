"""WAP-3: server-side origin gate on ``POST /api/plans/{plan_name}/ingest``.

The ingest-confirmation rule used to live ONLY in ``app/chat.py``'s system
prompt ("ask the human before ingesting"), which violates the plan doc's
server-side-confirmation invariant: a prompt is not an enforcement point.
After WAP-1/WAP-2 the chat service's internal HTTP client always stamps
``X-Pipeline-Origin: chat`` on its calls, so the ingest route can refuse
exactly those calls server-side while leaving the human single-operator
surface (the dashboard UI, which sends no origin header at all) untouched.

This module pins:

* ``POST /api/plans/{plan_name}/ingest`` with ``X-Pipeline-Origin: chat``
  returns 403 and NEVER touches ``_service.ingest_plan`` — the refusal must
  happen before any mutating work, and must not leak the plan name.
* the same route with NO origin header behaves byte-for-byte as before
  (legacy dashboard callers and the pre-existing ingest tests send none):
  the service is called exactly once with the plan name, its ``ok`` result is
  returned, and a not-``ok`` result is still mapped to 400 with its error.
* ``X-Pipeline-Origin: ui`` (and any other non-``chat`` value) passes the
  gate; the gate is case-sensitive like ``app.auth.refuse_chat_origin``.
* the route is still authenticated first: a bad API key is 401, not 403.
* the route declares ``x_pipeline_origin`` as a real FastAPI ``Header``
  aliased to ``X-Pipeline-Origin`` (so the gate cannot silently degrade into
  a query parameter that a caller never sets).
* ``save_plan`` is deliberately NOT gated in this story (ingest is the
  mutating confirmation point per the review).

The 403 detail is the generic ``"origin not permitted"`` from
``app.auth.refuse_chat_origin`` so the refusal leaks nothing about the
ingest machinery.
"""
from __future__ import annotations

import inspect
from typing import get_args, get_type_hints

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
import app.dashboard as dashboard_module
from app.auth import (
    ORIGIN_CHAT,
    ORIGIN_HEADER,
    ORIGIN_UI,
    get_or_create_api_key,
)

GENERIC_DETAIL = "origin not permitted"
INGEST_PATH = "/api/plans/some-plan/ingest"
SAVE_PATH = "/api/plans/some-plan/save"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _SpyService:
    """Records every call the routes make into the service."""

    def __init__(self, result: dict | None = None) -> None:
        self.result = {"ok": True, "ingested": 3} if result is None else result
        self.ingest_calls: list[tuple] = []
        self.save_plan_calls: list[tuple] = []

    def ingest_plan(self, plan_name, **kwargs):
        self.ingest_calls.append((plan_name, kwargs))
        return self.result

    def save_plan(self, plan_name, plan_json, workspace):
        self.save_plan_calls.append((plan_name, plan_json, workspace))
        return {"ok": True}

    def get_active_workspace(self):
        return "/tmp/active-workspace"


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


@pytest.fixture()
def failing_spy(monkeypatch):
    service = _SpyService(result={"ok": False, "error": "no manifest"})
    monkeypatch.setattr(dashboard_module, "_service", service)
    return service


def _origin_param_alias() -> str | None:
    """Resolve the ``X-Pipeline-Origin`` alias from the route signature.

    Works for both FastAPI idioms: ``Header(default=None, alias=...)`` (the
    alias lives on the parameter default) and
    ``Annotated[str | None, Header(alias=...)]`` (the alias lives in the
    resolved annotation metadata).
    """
    signature = inspect.signature(dashboard_module.ingest_plan)
    param = signature.parameters.get("x_pipeline_origin")
    assert param is not None, (
        "ingest_plan must declare an 'x_pipeline_origin' parameter"
    )

    alias = getattr(param.default, "alias", None)
    if alias:
        return alias

    resolved = get_type_hints(
        dashboard_module.ingest_plan, include_extras=True
    )
    for meta in get_args(resolved["x_pipeline_origin"])[1:]:
        if getattr(meta, "alias", None):
            return meta.alias
    return None


# --------------------------------------------------------------------------
# route: chat origin is refused and the service is never touched
# --------------------------------------------------------------------------


def test_ingest_refuses_chat_origin(spy):
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": GENERIC_DETAIL}
    assert spy.ingest_calls == []


def test_ingest_refuses_chat_origin_even_with_a_valid_body(spy):
    """The gate must fire before the body is even considered."""
    response = _client().post(
        INGEST_PATH,
        json={"only_epics": ["E1"], "overwrite": True},
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": GENERIC_DETAIL}
    assert spy.ingest_calls == []


def test_ingest_refusal_does_not_leak_the_plan_name(spy):
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 403
    body = response.text
    assert "some-plan" not in body
    assert "ingest" not in body.lower()


def test_ingest_chat_origin_is_still_authenticated_first(spy):
    """The origin gate must not become an auth bypass: a bad key is 401."""
    response = _client().post(
        INGEST_PATH,
        headers={
            "X-Pipeline-Api-Key": "not-the-real-key",
            ORIGIN_HEADER: ORIGIN_CHAT,
        },
    )

    assert response.status_code == 401
    assert spy.ingest_calls == []


# --------------------------------------------------------------------------
# route: absent origin keeps the legacy behaviour exactly
# --------------------------------------------------------------------------


def test_ingest_without_origin_header_reaches_the_service(spy):
    response = _client().post(INGEST_PATH, headers=_auth_headers())

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ingested": 3}
    assert spy.ingest_calls == [("some-plan", {})]


def test_ingest_without_origin_header_maps_not_ok_to_400(failing_spy):
    response = _client().post(INGEST_PATH, headers=_auth_headers())

    assert response.status_code == 400
    assert response.json() == {"detail": "no manifest"}
    assert failing_spy.ingest_calls == [("some-plan", {})]


def test_ingest_without_origin_header_forwards_body_options(spy):
    response = _client().post(
        INGEST_PATH,
        json={"only_epics": ["E1", "E2"], "overwrite": True},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [
        ("some-plan", {"only_epics": ["E1", "E2"], "overwrite": True})
    ]


def test_ingest_without_origin_header_defaults_body_options(spy):
    """An explicit empty body must still reach the service as before."""
    response = _client().post(INGEST_PATH, json={}, headers=_auth_headers())

    assert response.status_code == 200
    assert spy.ingest_calls == [
        ("some-plan", {"only_epics": None, "overwrite": False})
    ]


def test_ingest_without_origin_header_accepts_empty_only_epics(spy):
    """Boundary: an empty epic list is forwarded verbatim, not dropped."""
    response = _client().post(
        INGEST_PATH,
        json={"only_epics": [], "overwrite": False},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [
        ("some-plan", {"only_epics": [], "overwrite": False})
    ]


# --------------------------------------------------------------------------
# route: non-chat origins pass the gate
# --------------------------------------------------------------------------


def test_ingest_ui_origin_passes_the_gate(spy):
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_UI}),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [("some-plan", {})]


def test_ingest_unknown_origin_passes_the_gate(spy):
    """Only the exact ``chat`` value is refused (refuse_chat_origin)."""
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: "cli"}),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [("some-plan", {})]


def test_ingest_origin_gate_is_case_sensitive(spy):
    """``Chat`` is not the stamped value, so it passes like any other."""
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: "Chat"}),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [("some-plan", {})]


def test_ingest_empty_origin_header_passes_the_gate(spy):
    response = _client().post(
        INGEST_PATH,
        headers=_auth_headers(**{ORIGIN_HEADER: ""}),
    )

    assert response.status_code == 200
    assert spy.ingest_calls == [("some-plan", {})]


# --------------------------------------------------------------------------
# route wiring: the origin really is a header parameter
# --------------------------------------------------------------------------


def test_ingest_route_declares_the_origin_header_parameter():
    assert _origin_param_alias() == ORIGIN_HEADER


def test_ingest_route_exposes_the_origin_header_in_openapi():
    schema = dashboard_module.app.openapi()
    operation = schema["paths"]["/api/plans/{plan_name}/ingest"]["post"]
    header_params = [
        p for p in operation.get("parameters", []) if p.get("in") == "header"
    ]
    assert any(p.get("name") == ORIGIN_HEADER for p in header_params), (
        f"{ORIGIN_HEADER} must be an OpenAPI header parameter, got "
        f"{operation.get('parameters')}"
    )


def test_dashboard_module_imports_the_gate_helpers():
    """No duplicate imports: the helpers come from the existing import lines."""
    assert dashboard_module.refuse_chat_origin is not None
    assert dashboard_module.Header is not None


# --------------------------------------------------------------------------
# scope: the prompt-level rule stays; the server-side gate is added underneath
# --------------------------------------------------------------------------


def test_system_prompt_still_asks_the_model_to_confirm_before_ingesting():
    """This story adds enforcement UNDER the prompt sentence, not instead."""
    prompt = chat_module.SYSTEM_PROMPT
    assert "Always confirm with the user before calling ingest_plan" in prompt


# --------------------------------------------------------------------------
# scope: save_plan is deliberately NOT gated in this story
# --------------------------------------------------------------------------


def test_save_plan_is_not_gated_by_chat_origin(spy):
    response = _client().post(
        SAVE_PATH,
        json={"plan_json": "{\"epics\": []}", "workspace": "/tmp/ws"},
        headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT}),
    )

    assert response.status_code == 200
    assert spy.save_plan_calls == [
        ("some-plan", "{\"epics\": []}", "/tmp/ws")
    ]
