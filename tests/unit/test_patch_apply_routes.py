"""WAP-10: the patch REVIEW (GET) and APPLY (POST) dashboard routes.

This story adds two UI-only routes on top of the WAP-6 record store and the
WAP-7 apply engine:

* ``GET  /api/worktree/patch/{patch_id}`` -- hand the trusted UI the full
  stored record (including ``diff_text`` and the ``confirmation_token``) so a
  human can review the diff and then confirm it.
* ``POST /api/worktree/patch/{patch_id}/apply`` -- apply the SERVER-STORED
  record for that patch id, gated on the human confirmation token.

Both routes are UI-ONLY: the origin gate is ``require_ui_origin`` (the WAP-2
helper), so ``chat``, an absent header and any unknown value are all refused
with the generic 403 ``"origin not permitted"``.  This is the exact inverse of
the WAP-9 propose route's allow-list, and it is the review requirement that
makes the propose/apply split meaningful: the chat model may PROPOSE, but only
the ui-origin-authenticated human may APPLY.

The apply route re-accepts NOTHING but the token: the diff that gets applied is
always the server-stored record, never anything the caller sends.  A forged
``unified_diff`` in the apply body is therefore inert, and the engine is called
with exactly ``(plan_name, story_key, patch_id, confirmation_token)``.

Hermeticity: the manifest lives in a patched ``PLAN_DIR`` and the story's
worktree is a symlink-free temp git repo under ``tmp_path`` (resolved first,
because on macOS the raw temp path can run through ``/var -> /private/var`` and
a symlinked root component would make the write resolver reject every path for
the wrong reason).  Tests that must not reach git install a spy over
``pipeline.worktree_patch.apply_patch``.

These tests are self-contained and must fail for the right reason (missing
``ApplyPatchRequest`` / missing routes) until the implementation lands.
"""
from __future__ import annotations

import inspect
import json
import re
import subprocess

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.auth as auth_module
import app.chat as chat_module
import app.dashboard as dashboard_module
from app import dashboard_models
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, ORIGIN_UI, get_or_create_api_key
from pipeline import worktree_patch

PLAN_NAME = "wap10plan"
STORY_KEY = "WAP-10"

GET_PATH = "/api/worktree/patch/{patch_id}"
APPLY_PATH = "/api/worktree/patch/{patch_id}/apply"
PROPOSE_PATH = "/api/worktree/patch/propose"

GENERIC_DETAIL = "origin not permitted"
UNAUTHORIZED_DETAIL = "unauthorized"
NO_SUCH_PATCH_DETAIL = "no such patch"

#: A small, valid one-file diff: one context line, one delete, one add.
VALID_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " x = 1\n"
    "-y = 2\n"
    "+y = 3\n"
)

#: A forged diff a caller might try to smuggle into the apply body.
FORGED_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " x = 1\n"
    "-y = 2\n"
    "+y = 999\n"
)

#: The keys the GET route must hand the UI (the review payload).
GET_RECORD_KEYS = {
    "ok",
    "patch_id",
    "plan_name",
    "story_key",
    "paths",
    "added_lines",
    "diff_text",
    "status",
    "created_at",
    "expires_at",
    "confirmation_token",
}


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A private PLAN_DIR wired into every module that reads it.

    ``pipeline.store`` resolves ``PLAN_DIR`` through a ``LiveRef`` onto
    ``pipeline.server.PLAN_DIR``, so patching that binding is what makes the
    real ``dashboard_module._service`` see this test's manifest.
    """
    import pipeline.server as pserver
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(pserver, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    if hasattr(dashboard_module, "PLAN_DIR"):
        monkeypatch.setattr(dashboard_module, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree(tmp_path):
    """A symlink-free temp git repo to stand in for a story's worktree."""
    root = (tmp_path / "worktree").resolve()
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\ny = 2\n")
    subprocess.run(
        ["git", "init", "-q", str(root)], check=False, capture_output=True
    )
    return root


def _write_manifest(plan_dir, name, stories, epics=None):
    manifest = {"epics": epics or {}, "stories": stories}
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def manifest(plan_dir, worktree):
    """A manifest with one STUCK story (status ``failed``) and a real worktree."""
    _write_manifest(
        plan_dir,
        PLAN_NAME,
        {STORY_KEY: {"status": "failed", "worktree": str(worktree)}},
    )
    return plan_dir / f"{PLAN_NAME}.manifest.json"


@pytest.fixture
def client():
    return TestClient(dashboard_module.app)


@pytest.fixture(autouse=True)
def _clean_patch_store():
    """Keep the in-process record store from leaking between tests."""
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        store.clear()
    yield
    if isinstance(store, dict):
        store.clear()


def _auth_headers(**extra: str) -> dict:
    headers = {"X-Pipeline-Api-Key": get_or_create_api_key()}
    headers.update(extra)
    return headers


def _create_record(diff_text: str = VALID_DIFF, paths=None) -> dict:
    """Create a real pending record directly in the store; return its envelope."""
    return worktree_patch.create_patch_record(
        PLAN_NAME,
        STORY_KEY,
        diff_text,
        ["src/app.py"] if paths is None else paths,
        1,
    )


def _expire(patch_id: str) -> None:
    """Force a stored record's ``expires_at`` into the past."""
    from datetime import datetime, timedelta, timezone

    store = worktree_patch._PATCH_STORE
    store[patch_id]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()


class _ApplySpy:
    """Records every ``apply_patch`` call and returns a canned result."""

    def __init__(self, result: dict | None = None):
        self.result = result if result is not None else {
            "ok": True,
            "patch_id": "wp-spy",
            "applied": ["src/app.py"],
        }
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def spy(monkeypatch):
    """Install a spy over the apply engine (no git is ever reached)."""
    s = _ApplySpy()
    monkeypatch.setattr(worktree_patch, "apply_patch", s)
    return s


def _get(client, patch_id, *, origin=None, key=None):
    headers = {}
    if key is not None:
        headers["X-Pipeline-Api-Key"] = key
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    return client.get(GET_PATH.format(patch_id=patch_id), headers=headers)


def _apply(client, patch_id, *, origin=None, key=None, body=None):
    headers = {}
    if key is not None:
        headers["X-Pipeline-Api-Key"] = key
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    return client.post(
        APPLY_PATH.format(patch_id=patch_id),
        json={} if body is None else body,
        headers=headers,
    )


def _route(path: str, method: str):
    """Return the APIRoute registered for *path* + *method*, or None."""
    for route in dashboard_module.app.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return route
    return None


# =========================================================================== #
# Request model
# =========================================================================== #
class TestApplyPatchRequestModel:
    def test_model_exists(self):
        assert hasattr(dashboard_models, "ApplyPatchRequest")

    def test_model_has_exactly_one_declared_field(self):
        fields = dashboard_models.ApplyPatchRequest.model_fields
        assert set(fields) == {"confirmation_token"}

    def test_confirmation_token_is_a_required_str(self):
        field = dashboard_models.ApplyPatchRequest.model_fields["confirmation_token"]
        assert field.annotation is str
        assert field.is_required()

    def test_model_accepts_the_token(self):
        req = dashboard_models.ApplyPatchRequest(confirmation_token="tok")
        assert req.confirmation_token == "tok"

    def test_model_rejects_a_missing_token(self):
        with pytest.raises(ValidationError):
            dashboard_models.ApplyPatchRequest()

    def test_model_ignores_extra_fields_never_re_accepts_a_diff(self):
        """A forged diff in the body must not become a model field."""
        req = dashboard_models.ApplyPatchRequest(
            confirmation_token="tok", unified_diff=FORGED_DIFF
        )
        assert req.confirmation_token == "tok"
        assert not hasattr(req, "unified_diff")


# =========================================================================== #
# Route registration / signatures
# =========================================================================== #
class TestRouteRegistration:
    def test_get_route_is_registered(self):
        route = _route(GET_PATH, "GET")
        assert route is not None, "GET /api/worktree/patch/{patch_id} is missing"

    def test_apply_route_is_registered(self):
        route = _route(APPLY_PATH, "POST")
        assert route is not None, "POST /api/worktree/patch/{patch_id}/apply is missing"

    def test_get_route_takes_patch_id_and_origin_header(self):
        route = _route(GET_PATH, "GET")
        assert route is not None
        params = inspect.signature(route.endpoint).parameters
        assert "patch_id" in params
        assert "x_pipeline_origin" in params

    def test_apply_route_takes_patch_id_request_and_origin_header(self):
        route = _route(APPLY_PATH, "POST")
        assert route is not None
        params = inspect.signature(route.endpoint).parameters
        assert "patch_id" in params
        assert "request" in params
        assert "x_pipeline_origin" in params

    def test_apply_route_body_model_is_apply_patch_request(self):
        """The apply route's body is the token-only model, nothing else."""
        route = _route(APPLY_PATH, "POST")
        assert route is not None
        annotation = inspect.signature(route.endpoint, eval_str=True).parameters["request"].annotation
        assert annotation is dashboard_models.ApplyPatchRequest

    def test_both_routes_use_the_require_ui_origin_helper(self):
        """The WAP-2 helper is the gate, not a re-implemented origin check."""
        for path, method in ((GET_PATH, "GET"), (APPLY_PATH, "POST")):
            route = _route(path, method)
            assert route is not None, path
            assert "require_ui_origin" in inspect.getsource(route.endpoint), path

    def test_routes_are_appended_after_the_propose_route(self):
        """Append-only: both new routes come after the WAP-9 propose route."""
        paths = [getattr(r, "path", None) for r in dashboard_module.app.routes]
        assert PROPOSE_PATH in paths
        propose_idx = paths.index(PROPOSE_PATH)
        assert paths.index(GET_PATH) > propose_idx
        assert paths.index(APPLY_PATH) > propose_idx


# =========================================================================== #
# GET: the review payload
# =========================================================================== #
class TestGetPatchRoute:
    def test_ui_origin_returns_the_full_stored_record(self, client, manifest):
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _get(client, patch_id, origin=ORIGIN_UI, key=get_or_create_api_key())

        assert resp.status_code == 200
        body = resp.json()
        assert GET_RECORD_KEYS <= set(body), sorted(GET_RECORD_KEYS - set(body))
        assert body["ok"] is True
        assert body["patch_id"] == patch_id
        assert body["plan_name"] == PLAN_NAME
        assert body["story_key"] == STORY_KEY
        assert body["paths"] == ["src/app.py"]
        assert body["added_lines"] == 1
        assert body["diff_text"] == VALID_DIFF
        assert body["status"] == "pending"
        assert body["created_at"]
        assert body["expires_at"]

    def test_ui_origin_gets_the_usable_confirmation_token(self, client, manifest):
        """The token handed to the UI is the real, HMAC-bound token."""
        envelope = _create_record()
        resp = _get(
            client, envelope["patch_id"], origin=ORIGIN_UI, key=get_or_create_api_key()
        )
        assert resp.status_code == 200
        assert resp.json()["confirmation_token"] == envelope["confirmation_token"]

    def test_unknown_patch_id_is_404(self, client, manifest):
        resp = _get(client, "wp-does-not-exist", origin=ORIGIN_UI, key=get_or_create_api_key())
        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL

    def test_chat_origin_is_403(self, client, manifest):
        envelope = _create_record()
        resp = _get(client, envelope["patch_id"], origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    def test_absent_origin_is_403(self, client, manifest):
        envelope = _create_record()
        resp = _get(client, envelope["patch_id"], key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    def test_garbage_origin_is_403(self, client, manifest):
        envelope = _create_record()
        resp = _get(client, envelope["patch_id"], origin="garbage", key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    def test_expired_record_is_404(self, client, manifest):
        envelope = _create_record()
        _expire(envelope["patch_id"])
        resp = _get(client, envelope["patch_id"], origin=ORIGIN_UI, key=get_or_create_api_key())
        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL


# =========================================================================== #
# Apply: the origin gate (NEGATIVE TEST 4 -- the exact review scenario)
# =========================================================================== #
class TestApplyOriginGate:
    def test_chat_origin_with_valid_key_and_token_is_403_and_never_applies(
        self, client, manifest, spy
    ):
        """NEGATIVE TEST 4: the chat model holds a valid key AND the correct
        token, and is STILL refused -- and the engine is never invoked."""
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL
        assert spy.calls == []

    def test_absent_origin_is_403_and_never_applies(self, client, manifest, spy):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL
        assert spy.calls == []

    def test_garbage_origin_is_403_and_never_applies(self, client, manifest, spy):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin="garbage",
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL
        assert spy.calls == []

    def test_ui_origin_passes_the_gate_and_invokes_the_engine(
        self, client, manifest, spy
    ):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 200
        assert len(spy.calls) == 1
        args, _kwargs = spy.calls[0]
        assert args == (
            PLAN_NAME,
            STORY_KEY,
            envelope["patch_id"],
            envelope["confirmation_token"],
        )


# =========================================================================== #
# Apply: record lookup, token refusal, engine result mapping
# =========================================================================== #
class TestApplyEngineMapping:
    def test_fabricated_patch_id_is_404_and_engine_not_invoked(
        self, client, manifest, spy
    ):
        resp = _apply(
            client,
            "wp-fabricated",
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": "anything"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL
        assert spy.calls == []

    def test_expired_record_is_404(self, client, manifest, spy):
        envelope = _create_record()
        _expire(envelope["patch_id"])
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL
        assert spy.calls == []

    def test_wrong_confirmation_token_is_403_from_the_real_engine(
        self, client, manifest
    ):
        """The real engine refuses a wrong token; the route maps it to 403."""
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": "not-the-token"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "invalid confirmation token"
        # The refusal must not have consumed the record.
        assert worktree_patch.get_patch_record(envelope["patch_id"])["status"] == "pending"

    def test_token_from_another_patch_is_403(self, client, manifest):
        """The token is bound to patch_id + diff_hash: no cross-patch replay."""
        first = _create_record()
        second = _create_record()
        assert first["patch_id"] != second["patch_id"]
        resp = _apply(
            client,
            second["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": first["confirmation_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "invalid confirmation token"
        assert worktree_patch.get_patch_record(second["patch_id"])["status"] == "pending"

    def test_engine_receives_plan_and_story_from_the_record_not_the_request(
        self, client, spy
    ):
        """The patch id carries the plan: the route looks the record up first."""
        envelope = worktree_patch.create_patch_record(
            "other-plan", "OTHER-STORY", VALID_DIFF, ["src/app.py"], 1
        )
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 200
        assert len(spy.calls) == 1
        args, _kwargs = spy.calls[0]
        assert args == (
            "other-plan",
            "OTHER-STORY",
            envelope["patch_id"],
            envelope["confirmation_token"],
        )

    def test_empty_confirmation_token_is_403_from_the_real_engine(
        self, client, manifest
    ):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": ""},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "invalid confirmation token"

    def test_missing_confirmation_token_field_is_422(self, client, manifest, spy):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={},
        )
        assert resp.status_code == 422
        assert spy.calls == []

    def test_engine_refusal_status_code_and_error_are_mapped(
        self, client, manifest, monkeypatch
    ):
        """A non-ok engine result becomes HTTPException(status_code, error)."""
        envelope = _create_record()
        spy = _ApplySpy(
            {
                "ok": False,
                "error": "patch does not apply (context drift)",
                "status_code": 409,
            }
        )
        monkeypatch.setattr(worktree_patch, "apply_patch", spy)
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "patch does not apply (context drift)"

    def test_engine_success_result_is_returned_verbatim(
        self, client, manifest, monkeypatch
    ):
        envelope = _create_record()
        result = {"ok": True, "patch_id": envelope["patch_id"], "applied": ["src/app.py"]}
        monkeypatch.setattr(worktree_patch, "apply_patch", _ApplySpy(result))
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 200
        assert resp.json() == result


# =========================================================================== #
# Apply: the body never re-accepts diff content
# =========================================================================== #
class TestApplyNeverReacceptsDiffContent:
    def test_forged_unified_diff_is_inert_and_engine_gets_only_four_args(
        self, client, manifest, spy
    ):
        envelope = _create_record()
        patch_id = envelope["patch_id"]
        token = envelope["confirmation_token"]

        plain = _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": token},
        )
        forged = _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": token, "unified_diff": FORGED_DIFF},
        )

        assert plain.status_code == 200
        assert forged.status_code == plain.status_code
        assert forged.json() == plain.json()

        assert len(spy.calls) == 2
        for args, kwargs in spy.calls:
            assert args == (PLAN_NAME, STORY_KEY, patch_id, token)
            assert kwargs == {}
            assert FORGED_DIFF not in args

    def test_engine_call_has_no_diff_argument(self, client, manifest, spy):
        envelope = _create_record()
        _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": envelope["confirmation_token"], "diff_text": FORGED_DIFF},
        )
        assert len(spy.calls) == 1
        args, kwargs = spy.calls[0]
        assert len(args) == 4
        assert "diff" not in " ".join(kwargs)


# =========================================================================== #
# k- exemption pin (finding 4): the chat-stream exemption is route-exact
# =========================================================================== #
class TestChatStreamKeyExemptionDoesNotCoverNewRoutes:
    def test_chat_stream_path_constant_is_unchanged(self):
        assert auth_module.CHAT_STREAM_PATH == "/api/chat/stream"

    def test_k_prefixed_key_is_401_on_propose(self, client, manifest):
        resp = client.post(
            PROPOSE_PATH,
            json={"plan_name": PLAN_NAME, "story_key": STORY_KEY, "unified_diff": VALID_DIFF},
            headers={"X-Pipeline-Api-Key": "k-anything", ORIGIN_HEADER: ORIGIN_CHAT},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == UNAUTHORIZED_DETAIL

    def test_k_prefixed_key_is_401_on_get(self, client, manifest):
        envelope = _create_record()
        resp = _get(client, envelope["patch_id"], origin=ORIGIN_UI, key="k-anything")
        assert resp.status_code == 401
        assert resp.json()["detail"] == UNAUTHORIZED_DETAIL

    def test_k_prefixed_key_is_401_on_apply(self, client, manifest, spy):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_UI,
            key="k-anything",
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == UNAUTHORIZED_DETAIL
        assert spy.calls == []

    def test_k_prefixed_key_is_401_on_apply_with_chat_origin(self, client, manifest, spy):
        envelope = _create_record()
        resp = _apply(
            client,
            envelope["patch_id"],
            origin=ORIGIN_CHAT,
            key="k-anything",
            body={"confirmation_token": envelope["confirmation_token"]},
        )
        assert resp.status_code == 401
        assert spy.calls == []


# =========================================================================== #
# TOOLS exclusion: the apply route is UI-only, never a chat tool
# =========================================================================== #
class TestApplyRouteIsNotAChatTool:
    def test_no_chat_tool_name_mentions_apply(self):
        assert not any("apply" in name for name in chat_module.TOOLS)

    def test_propose_patch_is_still_a_chat_tool(self):
        assert "propose_patch" in chat_module.TOOLS

    def test_no_chat_tool_execute_url_touches_the_apply_route(self):
        for name, tool in chat_module.TOOLS.items():
            source = inspect.getsource(tool["execute"])
            assert "/apply" not in source, name
            matches = re.findall(r"/api/worktree/patch/[A-Za-z0-9_{}]*", source)
            assert all(m == "/api/worktree/patch/propose" for m in matches), (name, matches)

    def test_no_chat_tool_execute_url_reads_a_patch_record(self):
        """The GET review route is UI-only too: no tool fetches a patch id."""
        for name, tool in chat_module.TOOLS.items():
            source = inspect.getsource(tool["execute"])
            assert "patch_id" not in source, name
