"""Tests for ACT-04: the dashboard binds the authenticated principal for the
life of each request and records the actor on decisions.

TEN-WIRE-01 attached a ``Principal`` to ``request.state`` but nothing read it,
and the decisions route persisted only a *client-supplied* ``decided_by``. This
module pins the wiring end-to-end through a real ``TestClient`` so the
middleware itself is exercised, not just ``pipeline.actor``'s helpers:

* a request authenticated with the dashboard shared secret runs with
  ``LOCAL_OPERATOR`` bound, so ``actor_id()`` reports ``"local-operator"``;
* the chat-stream ``k-`` pass-through binds ``ANONYMOUS`` -- it is explicitly
  not an identity and must never be laundered into the local operator;
* ``POST /api/plans/{plan}/decisions`` records ``actor`` from the bound
  principal while ``decided_by`` keeps the caller-declared channel;
* the binding is request-scoped: it does not leak out of the request, so the
  test process is back to ``"local-operator"`` once the request completes.
"""
from contextlib import contextmanager

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from pipeline import actor

_HEADER = "X-Pipeline-Api-Key"
_PROBE_PATH = "/api/_test/actor"
_CHAT_STREAM_PATH = auth.CHAT_STREAM_PATH
_PLAN = "someplan"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@contextmanager
def _actor_probe(path):
    """Temporarily serve *path* with a route that reports the actor bound for
    the request, then remove it so no other test module sees the extra route.

    Inserted at position 0 so it shadows the real route at that path (the chat
    stream is SSE and cannot answer a plain GET); the middleware still runs
    first, which is what these tests are about.
    """
    async def _probe(request: Request):
        return {"actor": actor.actor_id()}

    route = APIRoute(path, _probe, methods=["GET"])
    d.app.router.routes.insert(0, route)
    try:
        yield
    finally:
        d.app.router.routes.remove(route)


def test_shared_secret_binds_local_operator(key_file):
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    with _actor_probe(_PROBE_PATH):
        response = client.get(_PROBE_PATH)

    assert response.status_code == 200
    assert response.json() == {"actor": "local-operator"}


def test_chat_stream_passthrough_binds_anonymous(key_file):
    """A caller-chosen k- value is a pass-through credential, not an identity:
    the request must not be attributed to the local operator."""
    client = TestClient(d.app, headers={_HEADER: "k-abc"})

    with _actor_probe(_CHAT_STREAM_PATH):
        response = client.get(_CHAT_STREAM_PATH)

    assert response.status_code == 200
    assert response.json() == {"actor": "anonymous"}


def test_decision_records_actor_and_keeps_client_decided_by(key_file):
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.post(
        f"/api/plans/{_PLAN}/decisions",
        json={
            "story_key": "ACT-04",
            "question": "Ship it?",
            "answer": "yes",
            "context": "actor binding",
            "decided_by": "human",
        },
    )

    assert response.status_code == 200
    record = response.json()["record"]
    assert record["actor"] == "local-operator"
    assert record["decided_by"] == "human"


def test_binding_does_not_leak_out_of_the_request(key_file):
    """The chat-stream request binds ANONYMOUS for its own duration only; the
    process must be back to the local operator afterwards."""
    client = TestClient(d.app, headers={_HEADER: "k-abc"})

    with _actor_probe(_CHAT_STREAM_PATH):
        response = client.get(_CHAT_STREAM_PATH)

    assert response.status_code == 200
    assert response.json() == {"actor": "anonymous"}
    assert actor.actor_id() == "local-operator"
