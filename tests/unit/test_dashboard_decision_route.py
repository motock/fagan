"""Tests for the dashboard's decision-request write route (W1c-01 follow-on).

Adds one POST route to app/dashboard.py that delegates to the module-level
``_service`` (a ``PipelineService`` singleton):

  POST /api/plans/{plan_name}/stories/{story_key}/decisions
        -> _service.request_decision(plan_name, story_key, question, options, context)

The JSON body is ``{"question": <string>, "options": [<string>, ...],
"context": <string, optional, default "">}``. ``context`` is optional and
defaults to the empty string ``""`` when omitted. The route returns the
service's result as-is with status 200. When ``options`` is an empty list the
route must reject the request (a 400/422 with a message), following the
file's existing validation-error convention.

The call invokes the overlord (an LLM call) and can take a while - it is
called synchronously with no timeout wrapper, matching the sibling routes.

These tests describe behavior for code that does not exist yet on this
branch and must fail (404 route-not-found from the TestClient, or
AttributeError on ``_service.request_decision`` in the delegation tests)
until app/dashboard.py is updated.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def mock_service(monkeypatch):
    """Replace the module-level _service singleton with a Mock so the new
    route can be exercised without touching the real pipeline store or
    invoking the overlord."""
    svc = Mock()
    monkeypatch.setattr(d, "_service", svc)
    return svc


def _call_args(call):
    """Return (positional_args, kwargs) for a recorded mock call."""
    return call.args, call.kwargs


# --- route registration ---------------------------------------------------


def test_decision_route_registered():
    paths = {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }
    assert ("/api/plans/{plan_name}/stories/{story_key}/decisions", "POST") in paths


# --- happy path -----------------------------------------------------------


def test_decision_200_delegates_to_service(client, mock_service):
    mock_service.request_decision.return_value = {
        "ok": True,
        "decision": "proceed",
        "rationale": "low risk",
    }

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={
            "question": "Should we proceed?",
            "options": ["yes", "no"],
            "context": "blocked on review",
        },
    )

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "decision": "proceed",
        "rationale": "low risk",
    }
    mock_service.request_decision.assert_called_once_with(
        "demo", "S1", "Should we proceed?", ["yes", "no"], "blocked on review"
    )


def test_decision_returns_service_result_as_is(client, mock_service):
    """The service's result must be returned verbatim (no reshaping/wrapping)."""
    raw = {"ok": True, "choice": "option-b", "extra": {"nested": [1, 2, 3]}}
    mock_service.request_decision.return_value = raw

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Pick one", "options": ["a", "b", "c"]},
    )

    assert res.status_code == 200
    assert res.json() == raw


# --- context defaulting ---------------------------------------------------


def test_decision_context_defaults_to_empty_string(client, mock_service):
    """`context` is optional and must default to "" when omitted from the body."""
    mock_service.request_decision.return_value = {"ok": True, "decision": "go"}

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": ["go", "stop"]},
    )

    assert res.status_code == 200
    mock_service.request_decision.assert_called_once_with(
        "demo", "S1", "Go?", ["go", "stop"], ""
    )


def test_decision_context_explicit_empty_string_passed_through(client, mock_service):
    mock_service.request_decision.return_value = {"ok": True, "decision": "go"}

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": ["go", "stop"], "context": ""},
    )

    assert res.status_code == 200
    mock_service.request_decision.assert_called_once_with(
        "demo", "S1", "Go?", ["go", "stop"], ""
    )


# --- validation: options ---------------------------------------------------


def test_decision_422_when_options_empty_list(client, mock_service):
    """An empty `options` list must be rejected (400/422 with a message)."""
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": []},
    )

    assert res.status_code in (400, 422)
    assert res.json().get("detail") or res.json().get("message"), (
        "expected a validation error message in the response body"
    )
    mock_service.request_decision.assert_not_called()


def test_decision_422_when_options_missing(client, mock_service):
    """`options` is required; omitting it must be rejected by body validation."""
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?"},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


def test_decision_422_when_options_not_a_list(client, mock_service):
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": "yes"},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


def test_decision_422_when_options_contains_non_string(client, mock_service):
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": ["yes", 42]},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


# --- validation: question --------------------------------------------------


def test_decision_422_when_question_missing(client, mock_service):
    """`question` is required; omitting it must be rejected by body validation."""
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"options": ["yes", "no"]},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


def test_decision_422_when_question_not_a_string(client, mock_service):
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": 123, "options": ["yes", "no"]},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


def test_decision_422_when_body_not_json_object(client, mock_service):
    """A non-object body (e.g. a bare string) is malformed for the
    {"question", "options"} contract and must be rejected."""
    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        content='"not-an-object"',
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 422
    mock_service.request_decision.assert_not_called()


# --- boundary: options cardinality -----------------------------------------


def test_decision_200_with_single_option(client, mock_service):
    """A single-element options list is a valid boundary value."""
    mock_service.request_decision.return_value = {"ok": True, "decision": "only"}

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Only?", "options": ["only"]},
    )

    assert res.status_code == 200
    mock_service.request_decision.assert_called_once_with(
        "demo", "S1", "Only?", ["only"], ""
    )


def test_decision_200_with_many_options(client, mock_service):
    options = [f"option-{i}" for i in range(20)]
    mock_service.request_decision.return_value = {"ok": True, "decision": options[0]}

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Many?", "options": options},
    )

    assert res.status_code == 200
    mock_service.request_decision.assert_called_once_with(
        "demo", "S1", "Many?", options, ""
    )


# --- service error handling (file convention) ------------------------------


def test_decision_raises_http_exception_when_service_reports_failure(
    client, mock_service
):
    """Following the file's existing convention, a service result with
    ok: False must raise HTTPException with the service's error message."""
    mock_service.request_decision.return_value = {
        "ok": False,
        "error": "overlord unavailable",
    }

    res = client.post(
        "/api/plans/demo/stories/S1/decisions",
        json={"question": "Go?", "options": ["go", "stop"]},
    )

    assert res.status_code in (400, 404, 422)
    assert "overlord unavailable" in res.json().get("detail", "")
