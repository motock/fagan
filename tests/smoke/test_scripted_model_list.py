"""SG-04: the scripted backend answers the model-list probes the drivers make.

An end-to-end run with ``provider: litellm`` pinned to the scripted backend
still lets the pipeline ask the *same* server two pre-flight questions:

- ``LMStudioProvider.reachable()`` does ``GET {base_url}/v1/models`` and treats
  any non-2xx as "unreachable";
- ``LMStudioProvider.loaded_models()`` does ``GET {base_url}/api/v0/models``
  and keeps the entries whose ``state`` is ``"loaded"``.

Before SG-04 both probes fell through the chat-completions dispatch to
``404 unknown endpoint``, so a scripted smoke run could read "unreachable" and
withhold dispatch or review. These tests pin the model-list contract against
the real :class:`app.inference_providers.LMStudioProvider` (no mocks on the
provider path): a probe that answers 404 is a false alarm, and the point of the
scripted backend is that nothing about a run's readiness is invented.

A model-list GET is a read-only probe: it must never consume a scripted answer,
so the per-role cursors are identical before and after.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

# ``pipeline.server`` is imported first: a new test module must route its
# ``pipeline.*`` imports through it (pre-existing import cycle) - same idiom as
# tests/smoke/test_scripted_model_server.py.
import pipeline.server  # noqa: F401
from app.inference_providers import LMStudioProvider
from tests.smoke import scripted_model_server as scripted

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def serve():
    """Start a scripted server on an ephemeral port; always stop it."""
    started = []

    def _start(script=None):
        server = scripted.ScriptedModelServer(script, host="127.0.0.1", port=0)
        server.start()
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()


def _request(server, path: str, *, method: str = "GET", body: bytes | None = None):
    """Issue one request; return (status, decoded JSON object or {})."""
    request = urllib.request.Request(
        server.base_url + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _decode(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _model_ids(payload: dict) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [entry.get("id") for entry in data if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# GET /v1/models - the reachability probe
# ---------------------------------------------------------------------------


def test_get_v1_models_lists_one_model_per_role(serve):
    server = serve()

    status, payload = _request(server, "/v1/models")

    assert status == 200
    assert payload.get("object") == "list"
    ids = set(_model_ids(payload))
    assert {
        "scripted-dispatch",
        "scripted-review",
        "scripted-overlord",
        "scripted-decompose",
    } <= ids


# ---------------------------------------------------------------------------
# GET /api/v0/models - the loaded-model probe
# ---------------------------------------------------------------------------


def test_get_api_v0_models_reports_every_entry_as_loaded(serve):
    server = serve()

    status, payload = _request(server, "/api/v0/models")

    assert status == 200
    assert payload.get("object") == "list"
    data = payload.get("data") or []
    assert data, "the model list must not be empty"
    assert all(entry.get("state") == "loaded" for entry in data)


# ---------------------------------------------------------------------------
# Integration: the real provider against the real server
# ---------------------------------------------------------------------------


def test_lmstudio_provider_probes_the_scripted_backend(serve):
    server = serve()
    provider = LMStudioProvider()

    assert provider.reachable(server.base_url) == (True, "")
    assert "scripted-dispatch" in provider.loaded_models(server.base_url)


# ---------------------------------------------------------------------------
# A model-list GET is read-only: it must not consume a scripted answer
# ---------------------------------------------------------------------------


def test_model_list_gets_leave_every_role_cursor_alone(serve):
    server = serve()
    before = server.cursor_state()

    assert _request(server, "/v1/models")[0] == 200
    assert _request(server, "/api/v0/models")[0] == 200

    assert server.cursor_state() == before


# ---------------------------------------------------------------------------
# Negative: every other request keeps today's behaviour
# ---------------------------------------------------------------------------


def test_unknown_get_path_is_still_404(serve):
    server = serve()

    status, _ = _request(server, "/v1/unknown")

    assert status == 404


def test_get_on_the_chat_path_is_still_405(serve):
    server = serve()

    status, _ = _request(server, scripted.ENDPOINT_PATH)

    assert status == 405


def test_post_to_a_model_list_path_is_still_404(serve):
    server = serve()

    status, _ = _request(
        server,
        "/v1/models",
        method="POST",
        body=json.dumps({"model": "openai/scripted-dispatch"}).encode("utf-8"),
    )

    assert status == 404
