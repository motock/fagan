"""Tests for C2: one correlation id per dashboard request.

The dashboard middleware mints (or adopts) exactly one correlation id per
inbound request, echoes it on every response -- including the 401/403
refusals -- binds it for the life of the request via ``pipeline.actor``'s
contextvar, and the records the request writes (decisions, approvals) are
stamped with it.

The binding is graded, not just the echo: the probe route below reports
``current_correlation_id()`` from inside the request, so a middleware that
sets the response header without binding (or binds without resetting) fails
here. The id is carried by the contextvar, never by an environment variable:
the environment is process-global and two dashboard requests in flight would
clobber each other's id.
"""
import asyncio
import json

import pytest
from fastapi import Request, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from pipeline import persistence, plan_approval, server
from pipeline.actor import (
    CORRELATION_ID_RE,
    bind_correlation_id,
    current_correlation_id,
)
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService

_HEADER = "X-Pipeline-Api-Key"
_CORRELATION_HEADER = "X-Correlation-ID"
_PROBE_PATH = "/api/_test/correlation"
_NON_CHAT_PATH = "/api/status"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def correlation_probe():
    """Register a test-only /api/ route that reports the id bound for the
    request, then remove it so no other test module sees the extra route."""
    async def _probe(request: Request):
        return {"correlation_id": current_correlation_id()}

    route = APIRoute(_PROBE_PATH, _probe, methods=["GET"])
    d.app.router.routes.insert(0, route)
    try:
        yield
    finally:
        d.app.router.routes.remove(route)


def _client(**kwargs):
    """A real TestClient carrying the dashboard secret (the gate is on)."""
    headers = {_HEADER: auth.get_or_create_api_key()}
    headers.update(kwargs.pop("headers", {}))
    return TestClient(d.app, headers=headers, **kwargs)


def _request(path, *, host="testserver", api_key=None, correlation=None,
             server=("testserver", 80)):
    """Build a Starlette Request the middleware can run against directly.

    Used where a TestClient cannot express the case: a disallowed Host header
    while the ASGI server host stays ``testserver``, and the same-thread
    no-leak check (a TestClient runs the app in its own thread, so a leaked
    contextvar would not be visible to the test process).
    """
    headers = [(b"host", host.encode("latin-1"))]
    if api_key is not None:
        headers.append((b"x-pipeline-api-key", api_key.encode("latin-1")))
    if correlation is not None:
        headers.append((b"x-correlation-id", correlation.encode("latin-1")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "server": server,
        "client": ("127.0.0.1", 54321),
    }
    return Request(scope)


async def _call_next(request):
    return Response("reached-the-route")


def _run_middleware(request):
    return asyncio.run(d._enforce_api_key(request, _call_next))


def _drive_in_current_context(request):
    """Run the middleware coroutine in *this* context, not a copy.

    ``asyncio.run`` runs the coroutine in a copied context, so a binding the
    middleware failed to reset would be invisible to the caller and a
    ``current_correlation_id() == ""`` assertion after it could never fail.
    Stepping the coroutine by hand keeps it in the caller's context, which
    makes the leak assertions below genuinely grade the reset.
    """
    coro = d._enforce_api_key(request, _call_next)
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    finally:
        coro.close()
    raise AssertionError("middleware suspended; expected it to complete inline")


# --------------------------------------------------------------------------- #
# Minting and echoing
# --------------------------------------------------------------------------- #

def test_request_without_header_gets_a_minted_id(key_file, correlation_probe):
    response = _client().get(_PROBE_PATH)

    assert response.status_code == 200
    correlation = response.headers[_CORRELATION_HEADER]
    assert CORRELATION_ID_RE.fullmatch(correlation)
    assert len(correlation) == 12


def test_well_formed_upstream_id_is_echoed(key_file, correlation_probe):
    response = _client().get(
        _PROBE_PATH, headers={_CORRELATION_HEADER: "abc-123"}
    )

    assert response.status_code == 200
    assert response.headers[_CORRELATION_HEADER] == "abc-123"


@pytest.mark.parametrize("forged", ["bad id\ninjected", "a" * 65])
def test_forged_header_is_replaced_by_a_minted_id(key_file, correlation_probe, forged):
    assert CORRELATION_ID_RE.fullmatch(forged) is None

    response = _client().get(
        _PROBE_PATH, headers={_CORRELATION_HEADER: forged}
    )

    assert response.status_code == 200
    correlation = response.headers[_CORRELATION_HEADER]
    assert CORRELATION_ID_RE.fullmatch(correlation)
    assert len(correlation) == 12
    assert correlation != forged
    assert forged not in correlation


# --------------------------------------------------------------------------- #
# The binding, not just the echo
# --------------------------------------------------------------------------- #

def test_probe_reports_the_id_bound_for_the_request(key_file, correlation_probe):
    response = _client().get(
        _PROBE_PATH, headers={_CORRELATION_HEADER: "abc-123"}
    )

    assert response.json()["correlation_id"] == "abc-123"
    assert response.json()["correlation_id"] == response.headers[_CORRELATION_HEADER]


def test_probe_reports_the_minted_id_when_no_header_was_sent(
    key_file, correlation_probe
):
    response = _client().get(_PROBE_PATH)

    assert response.json()["correlation_id"] == response.headers[_CORRELATION_HEADER]


def test_binding_does_not_leak_between_requests(key_file):
    """Two requests in the same context: the second must not inherit the
    first's id, and nothing is bound once each returns."""
    api_key = auth.get_or_create_api_key()

    first = _request(_NON_CHAT_PATH, api_key=api_key, correlation="abc-123")
    first_response = _drive_in_current_context(first)
    assert first_response.headers["x-correlation-id"] == "abc-123"
    assert current_correlation_id() == ""

    second = _request(_NON_CHAT_PATH, api_key=api_key)
    second_response = _drive_in_current_context(second)
    fresh = second_response.headers["x-correlation-id"]
    assert fresh != "abc-123"
    assert len(fresh) == 12
    assert current_correlation_id() == ""


def test_binding_does_not_leak_after_a_testclient_request(key_file, correlation_probe):
    """A real request must not leave an id bound for whatever runs next.

    The follow-up request is the observable: it must get its own id rather than
    inheriting the previous one. (A bare ``current_correlation_id() == ""`` here
    would be vacuous -- TestClient runs the app in a copied context.)
    """
    client = _client()
    client.get(_PROBE_PATH, headers={_CORRELATION_HEADER: "abc-123"})

    follow_up = client.get(_PROBE_PATH)

    assert follow_up.json()["correlation_id"] == follow_up.headers[_CORRELATION_HEADER]
    assert follow_up.headers[_CORRELATION_HEADER] != "abc-123"


# --------------------------------------------------------------------------- #
# Refusals carry the id; their bodies are unchanged
# --------------------------------------------------------------------------- #

def test_disallowed_host_403_carries_the_id_and_keeps_its_body(key_file):
    request = _request(
        _NON_CHAT_PATH,
        host="evil.example.com",
        api_key=auth.get_or_create_api_key(),
    )

    response = _run_middleware(request)

    assert response.status_code == 403
    assert response.body == b'{"detail":"Forbidden"}'
    assert CORRELATION_ID_RE.fullmatch(response.headers["x-correlation-id"])


def test_bad_api_key_401_carries_the_id_and_keeps_its_body(key_file):
    auth.get_or_create_api_key()
    client = TestClient(d.app)
    # tests/unit/conftest.py's _authenticated_test_client attaches the key to
    # every TestClient; popping it is the sanctioned way to exercise the
    # denial path through the real ASGI stack.
    client.headers.pop(_HEADER)

    response = client.get(_NON_CHAT_PATH)

    assert response.status_code == 401
    assert response.content == b'{"detail":"unauthorized"}'
    assert CORRELATION_ID_RE.fullmatch(response.headers[_CORRELATION_HEADER])


# --------------------------------------------------------------------------- #
# The records the request writes are stamped with the id
# --------------------------------------------------------------------------- #

def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {},
        "context": "ctx",
        "stories": {
            "S1": {
                "summary": "do the thing",
                "agent_instructions": "instructions",
                "dependencies": [],
                "persona": "engineer",
                "model": None,
                "acceptance": ["it works"],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": ["a.py"],
                "blocked_by": [],
                "status": "todo",
            }
        },
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Write a manifest for plan ``p`` and point both PLAN_DIR bindings at it."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    (tmp_path / "p.manifest.json").write_text(json.dumps(_manifest()))
    return tmp_path


@pytest.fixture
def pending_plan(tmp_path, monkeypatch):
    """Write a manifest whose approval record is pending, digest included."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    manifest = _manifest()
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": canonical_digest(manifest),
        "warnings_seen": ["w1"],
    }
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.fixture
def approval_enabled(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


def test_decision_record_is_stamped_with_the_request_id(key_file, plan_dir):
    response = _client().post(
        "/api/plans/p/decisions",
        headers={_CORRELATION_HEADER: "abc-123"},
        json={
            "story_key": "S1",
            "question": "ship it?",
            "answer": "yes",
            "context": "because",
            "decided_by": "alice",
        },
    )

    assert response.status_code == 200
    record = response.json()["record"]
    assert record["correlation_id"] == "abc-123"
    assert record["correlation_id"] == response.headers[_CORRELATION_HEADER]
    # The client-declared decider is preserved; the id is not an identity.
    assert record["decided_by"] == "alice"


def test_decision_record_without_an_inbound_header_gets_the_minted_id(
    key_file, plan_dir
):
    response = _client().post(
        "/api/plans/p/decisions",
        json={
            "story_key": "S1",
            "question": "ship it?",
            "answer": "yes",
        },
    )

    assert response.status_code == 200
    record = response.json()["record"]
    assert record["correlation_id"] == response.headers[_CORRELATION_HEADER]
    assert len(record["correlation_id"]) == 12


def test_approval_records_the_bound_correlation_id(
    pending_plan, approval_enabled
):
    with bind_correlation_id("req-9"):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert _read_manifest(pending_plan)["approval"]["correlation_id"] == "req-9"

    # The binding was reset, not left behind: the next approval records "".
    PipelineService().approve_plan("p")
    assert _read_manifest(pending_plan)["approval"]["correlation_id"] == ""


def test_approval_with_nothing_bound_records_an_empty_id(
    pending_plan, approval_enabled
):
    result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert _read_manifest(pending_plan)["approval"]["correlation_id"] == ""


# --------------------------------------------------------------------------- #
# The id is decision metadata: preserved on re-ingest, cleared on reset
# --------------------------------------------------------------------------- #

def test_reingest_of_unchanged_content_preserves_the_correlation_id():
    manifest = _manifest()
    manifest["approval"] = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "decided_by": "alice",
        "at": "2026-01-01T00:00:00+00:00",
        "correlation_id": "req-9",
        "role": "local-operator",
        "self_approved": False,
        "warnings_seen": [],
    }

    record = plan_approval.on_ingest(manifest)

    assert record["status"] == "approved"
    assert record["correlation_id"] == "req-9"


def test_reset_clears_the_correlation_id_of_an_invalidated_decision(
    approval_enabled,
):
    manifest = _manifest()
    manifest["approval"] = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "decided_by": "alice",
        "at": "2026-01-01T00:00:00+00:00",
        "correlation_id": "req-9",
        "role": "local-operator",
        "self_approved": False,
        "warnings_seen": [],
    }
    manifest["stories"]["S1"]["agent_instructions"] = "changed"

    assert plan_approval.reset_if_changed(manifest) is True

    approval = manifest["approval"]
    assert approval["status"] == "pending_approval"
    assert "correlation_id" not in approval
    assert "decided_by" not in approval
