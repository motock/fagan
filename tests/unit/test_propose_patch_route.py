"""WAP-9: ``POST /api/worktree/patch/propose`` — the propose route + chat tool.

This story wires the WAP-5 propose validator and the WAP-6 patch-record store
into an HTTP route the chat model can reach, and registers the matching
``propose_patch`` tool in ``app/chat.py``'s ``TOOLS`` registry.

The route is chat-reachable BY DESIGN (unlike ``set_workspace``/``ingest``,
which refuse chat origin): proposing a patch is how the model asks a human to
review its work.  So the origin gate here is an ALLOW-list — exactly
``ORIGIN_CHAT`` or ``ORIGIN_UI`` pass, everything else (absent, unknown,
wrong case) is refused with the generic 403 ``"origin not permitted"``.

The propose/apply split is a fixed review requirement and is pinned below: a
patch touching ``.git`` IS accepted at propose (the human must be able to
inspect what the model tried) and refused later at apply time (WAP-10/WAP-11).

Hermeticity: the manifest lives in a patched ``PLAN_DIR`` and the story's
worktree is a symlink-free temp git repo under ``tmp_path`` (resolved first,
because on macOS the raw temp path can run through ``/var -> /private/var``
and a symlinked root component would make the read-half resolver reject every
path for the wrong reason).

These tests are self-contained and must fail for the right reason (missing
``ProposePatchRequest`` / missing route / missing registry entry) until the
implementation lands.
"""
from __future__ import annotations

import inspect
import json
import subprocess
from typing import get_type_hints

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.chat as chat_module
import app.dashboard as dashboard_module
from app import dashboard_models
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, ORIGIN_UI, get_or_create_api_key
from pipeline import worktree_patch

PROPOSE_PATH = "/api/worktree/patch/propose"
GENERIC_DETAIL = "origin not permitted"
PLAN_NAME = "wapplan"
STORY_KEY = "S1"

# --------------------------------------------------------------------------
# diffs
# --------------------------------------------------------------------------

#: A small, valid one-file diff: one context line, one delete, one add.
VALID_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " x = 1\n"
    "-y = 2\n"
    "+y = 3\n"
)

#: A hunk whose new-side path escapes the worktree root.
ESCAPE_DIFF = (
    "--- a/../escape.txt\n"
    "+++ b/../escape.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " ctx\n"
    "-old\n"
    "+new\n"
)

#: A hunk whose new-side path is absolute.
ABSOLUTE_DIFF = (
    "--- a/etc/passwd\n"
    "+++ /etc/passwd\n"
    "@@ -1,2 +1,2 @@\n"
    " ctx\n"
    "-old\n"
    "+new\n"
)

#: A diff touching ``.git/config`` — DENIED at apply, ACCEPTED at propose.
GIT_DIFF = (
    "--- a/.git/config\n"
    "+++ b/.git/config\n"
    "@@ -1,2 +1,2 @@\n"
    " ctx\n"
    "-old\n"
    "+new\n"
)


def _six_file_diff() -> str:
    """Six distinct new-side paths: one over the 5-file propose limit."""
    return "".join(
        f"--- a/f{i}.txt\n"
        f"+++ b/f{i}.txt\n"
        f"@@ -1,2 +1,2 @@\n"
        f" ctx\n"
        f"-old\n"
        f"+new\n"
        for i in range(6)
    )


def _too_many_added_lines_diff() -> str:
    """A new-file block with 401 added lines: one over the 400-line limit."""
    return (
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,401 @@\n"
        + "".join(f"+line{i}\n" for i in range(401))
    )


def _oversized_diff() -> str:
    """A structurally valid diff whose byte size exceeds the 65536 limit."""
    return (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,1 +1,1 @@\n"
        " " + "x" * 70000 + "\n"
    )


def _diff_of_exactly_bytes(target_bytes: int) -> str:
    """A structurally valid diff whose UTF-8 length is exactly *target_bytes*."""
    head = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,1 +1,1 @@\n "
    tail = "\n"
    pad = target_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    assert pad >= 0, target_bytes
    diff = head + "x" * pad + tail
    assert len(diff.encode("utf-8")) == target_bytes
    return diff


def _n_file_diff(n: int) -> str:
    """A diff touching exactly *n* distinct new-side paths."""
    return "".join(
        f"--- a/f{i}.txt\n"
        f"+++ b/f{i}.txt\n"
        f"@@ -1,2 +1,2 @@\n"
        f" ctx\n"
        f"-old\n"
        f"+new\n"
        for i in range(n)
    )


def _new_file_diff(added: int) -> str:
    """A pure-addition new-file block with exactly *added* ``+`` lines."""
    return (
        "--- /dev/null\n"
        f"+++ b/new.txt\n"
        f"@@ -0,0 +1,{added} @@\n"
        + "".join(f"+line{i}\n" for i in range(added))
    )


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
    (root / "src" / "app.py").write_text("x = 1\n")
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


def _auth_headers(**extra: str) -> dict:
    headers = {"X-Pipeline-Api-Key": get_or_create_api_key()}
    headers.update(extra)
    return headers


def _body(**overrides) -> dict:
    body = {
        "plan_name": PLAN_NAME,
        "story_key": STORY_KEY,
        "unified_diff": VALID_DIFF,
    }
    body.update(overrides)
    return body


def _post(client, *, origin=None, key=None, body=None, **kwargs):
    headers = {}
    if key is not None:
        headers["X-Pipeline-Api-Key"] = key
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    return client.post(
        PROPOSE_PATH,
        json=_body() if body is None else body,
        headers=headers,
        **kwargs,
    )


def _store_snapshot() -> dict:
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    return dict(store) if isinstance(store, dict) else {}


# =========================================================================== #
# Request model
# =========================================================================== #
class TestProposePatchRequestModel:
    def test_model_exists(self):
        assert hasattr(dashboard_models, "ProposePatchRequest")

    def test_model_has_exactly_the_three_required_str_fields(self):
        fields = dashboard_models.ProposePatchRequest.model_fields
        assert set(fields) == {"plan_name", "story_key", "unified_diff"}
        for name in ("plan_name", "story_key", "unified_diff"):
            assert fields[name].annotation is str, name
            assert fields[name].is_required(), name

    def test_model_accepts_the_three_fields(self):
        req = dashboard_models.ProposePatchRequest(
            plan_name="p", story_key="S1", unified_diff="d"
        )
        assert req.plan_name == "p"
        assert req.story_key == "S1"
        assert req.unified_diff == "d"

    @pytest.mark.parametrize("missing", ["plan_name", "story_key", "unified_diff"])
    def test_model_rejects_a_missing_field(self, missing):
        payload = {"plan_name": "p", "story_key": "S1", "unified_diff": "d"}
        payload.pop(missing)
        with pytest.raises(ValidationError):
            dashboard_models.ProposePatchRequest(**payload)


# =========================================================================== #
# Route registration / signature
# =========================================================================== #
class TestRouteRegistration:
    def test_route_function_exists(self):
        assert hasattr(dashboard_module, "propose_worktree_patch_route")

    def test_route_is_registered_as_post_on_the_exact_path(self):
        schema = dashboard_module.app.openapi()
        assert PROPOSE_PATH in schema["paths"], sorted(schema["paths"])
        assert "post" in schema["paths"][PROPOSE_PATH]

    def test_route_request_param_is_annotated_with_the_model(self):
        hints = get_type_hints(dashboard_module.propose_worktree_patch_route)
        assert hints.get("request") is dashboard_models.ProposePatchRequest

    def test_dashboard_reexports_the_request_model(self):
        # dashboard.py re-imports every name from dashboard_models, so the
        # route's annotation resolves in that namespace.
        assert (
            dashboard_module.ProposePatchRequest
            is dashboard_models.ProposePatchRequest
        )

    def test_route_declares_the_origin_header_alias(self):
        assert _origin_param_alias() == ORIGIN_HEADER

    def test_route_exposes_the_origin_header_in_openapi(self):
        schema = dashboard_module.app.openapi()
        params = schema["paths"][PROPOSE_PATH]["post"].get("parameters", [])
        assert ORIGIN_HEADER in {p.get("name") for p in params}


def _origin_param_alias() -> str | None:
    """Resolve the ``X-Pipeline-Origin`` alias from the route signature.

    Handles both FastAPI idioms: ``Header(default=None, alias=...)`` (the
    alias lives on the parameter default) and
    ``Annotated[str | None, Header(alias=...)]`` (the alias lives in the
    annotation metadata).
    """
    signature = inspect.signature(dashboard_module.propose_worktree_patch_route)
    param = signature.parameters.get("x_pipeline_origin")
    if param is None:
        return None
    alias = getattr(param.default, "alias", None)
    if alias:
        return alias
    for meta in getattr(param.annotation, "__metadata__", ()):
        if getattr(meta, "alias", None):
            return meta.alias
    return None


def _route_source() -> str:
    return inspect.getsource(dashboard_module.propose_worktree_patch_route)


# =========================================================================== #
# Append-only placement and the inline origin gate
# =========================================================================== #
class TestAppendOnlyAndInlineGate:
    def test_route_is_appended_after_the_last_pre_existing_route(self):
        # dashboard.py is append-only for this story: the new route must be
        # defined AFTER the last pre-existing route (``dashboard_index``).
        new_line = inspect.getsourcelines(
            dashboard_module.propose_worktree_patch_route
        )[1]
        last_existing = inspect.getsourcelines(dashboard_module.dashboard_index)[1]
        assert new_line > last_existing

    def test_route_uses_the_auth_origin_constants_inline(self):
        source = _route_source()
        assert "ORIGIN_CHAT" in source
        assert "ORIGIN_UI" in source

    def test_no_new_propose_origin_helper_was_added_to_auth(self):
        import app.auth as auth_module

        offenders = [
            name
            for name, value in vars(auth_module).items()
            if callable(value)
            and not name.startswith("_")
            and "propose" in name.lower()
        ]
        assert offenders == [], offenders

    def test_route_uses_the_service_manifest_accessor(self):
        assert "get_manifest_or_none" in _route_source()

    def test_route_calls_the_propose_validator_and_the_record_store(self):
        source = _route_source()
        assert "validate_for_propose" in source
        assert "create_patch_record" in source

    def test_route_does_not_enforce_the_deny_list_at_propose(self):
        # The propose/apply split is a fixed review requirement: the deny
        # list is APPLY-side only, so the route must not CALL it.
        assert "is_denied_relative_path(" not in _route_source()

    def test_route_maps_patch_format_error_to_413(self):
        source = _route_source()
        assert "PatchFormatError" in source
        assert "413" in source

    def test_route_maps_workspace_security_error_to_400(self):
        source = _route_source()
        assert "WorkspaceSecurityError" in source
        assert "400" in source


# =========================================================================== #
# Happy path (chat origin IS the success case)
# =========================================================================== #
class TestHappyPath:
    def test_chat_origin_proposes_and_stores_the_record(self, client, manifest):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["ok"] is True
        assert isinstance(body["patch_id"], str) and body["patch_id"]
        assert body["paths"] == ["src/app.py"]
        assert body["added_lines"] == 1
        assert isinstance(body["confirmation_token"], str) and body["confirmation_token"]
        assert isinstance(body["diff_hash"], str) and body["diff_hash"]
        assert set(body) >= {
            "ok",
            "patch_id",
            "paths",
            "added_lines",
            "confirmation_token",
            "diff_hash",
        }

        record = worktree_patch.get_patch_record(body["patch_id"])
        assert record is not None
        assert record["diff_text"] == VALID_DIFF
        assert record["plan_name"] == PLAN_NAME
        assert record["story_key"] == STORY_KEY
        assert record["paths"] == ["src/app.py"]
        assert record["added_lines"] == 1

    def test_ui_origin_is_also_allowed(self, client, manifest):
        resp = _post(client, origin=ORIGIN_UI, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

    def test_response_body_is_the_store_result_dict(self, client, manifest):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        body = resp.json()
        record = worktree_patch.get_patch_record(body["patch_id"])
        assert record["diff_hash"] == body["diff_hash"]
        assert record["status"] == "pending"


# =========================================================================== #
# Origin gate (allow-list: chat | ui)
# =========================================================================== #
class TestOriginGate:
    def test_absent_origin_is_refused(self, client, manifest):
        resp = _post(client, key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    @pytest.mark.parametrize("origin", ["garbage", "", "Chat", "CHAT", "dashboard"])
    def test_unknown_origin_is_refused(self, client, manifest, origin):
        resp = _post(client, origin=origin, key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    def test_origin_gate_runs_before_the_story_lookup(self, client, plan_dir):
        # No manifest at all: an absent origin must still be 403, not 404.
        resp = _post(client, key=get_or_create_api_key())
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL

    def test_origin_gate_creates_no_record(self, client, manifest):
        before = _store_snapshot()
        resp = _post(client, key=get_or_create_api_key())
        assert resp.status_code == 403
        assert _store_snapshot() == before


# =========================================================================== #
# Auth
# =========================================================================== #
class TestAuth:
    def test_wrong_api_key_is_401(self, client, manifest):
        resp = _post(client, origin=ORIGIN_CHAT, key="not-the-key")
        assert resp.status_code == 401

    def test_missing_api_key_is_401(self, client, manifest):
        # The suite's autouse `_authenticated_test_client` fixture attaches
        # the dashboard key to every TestClient, so drop it to exercise the
        # denial path (per the fixture's own docstring).
        client.headers.pop("X-Pipeline-Api-Key", None)
        resp = _post(client, origin=ORIGIN_CHAT)
        assert resp.status_code == 401

    def test_auth_runs_before_the_origin_gate(self, client, manifest):
        # Wrong key AND a refused origin: auth wins, so 401 not 403.
        resp = _post(client, key="not-the-key")
        assert resp.status_code == 401


# =========================================================================== #
# Propose limits -> 413
# =========================================================================== #
class TestProposeLimits:
    def test_oversized_diff_is_413(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_oversized_diff()),
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"]

    def test_six_file_diff_is_413(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_six_file_diff()),
        )
        assert resp.status_code == 413
        assert "too many files" in resp.json()["detail"]

    def test_too_many_added_lines_is_413(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_too_many_added_lines_diff()),
        )
        assert resp.status_code == 413
        assert "too many added lines" in resp.json()["detail"]

    def test_refused_diff_creates_no_record(self, client, manifest):
        before = _store_snapshot()
        _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_oversized_diff()),
        )
        assert _store_snapshot() == before


# =========================================================================== #
# Path security at propose -> 400
# =========================================================================== #
class TestPathSecurity:
    def test_escaping_hunk_path_is_400(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=ESCAPE_DIFF),
        )
        assert resp.status_code == 400
        assert ".." in resp.json()["detail"]

    def test_absolute_hunk_path_is_400(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=ABSOLUTE_DIFF),
        )
        assert resp.status_code == 400
        assert "absolute" in resp.json()["detail"]

    def test_refused_path_creates_no_record(self, client, manifest):
        before = _store_snapshot()
        _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=ESCAPE_DIFF),
        )
        assert _store_snapshot() == before


# =========================================================================== #
# Deny-list acceptance pin (propose accepts .git; apply refuses it)
# =========================================================================== #
class TestDenyListAcceptedAtPropose:
    def test_git_touching_diff_is_accepted_and_stored(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=GIT_DIFF),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["paths"] == [".git/config"]
        record = worktree_patch.get_patch_record(body["patch_id"])
        assert record is not None
        assert record["diff_text"] == GIT_DIFF


# =========================================================================== #
# Story / plan lookup and the stuck-only gate
# =========================================================================== #
class TestStoryLookup:
    def test_unknown_plan_is_404(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(plan_name="no-such-plan"),
        )
        assert resp.status_code == 404

    def test_unknown_story_key_is_404(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(story_key="NOPE"),
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize("status", ["in_progress", "running"])
    def test_non_stuck_story_is_409_and_creates_no_record(
        self, client, plan_dir, worktree, status
    ):
        _write_manifest(
            plan_dir,
            PLAN_NAME,
            {STORY_KEY: {"status": status, "worktree": str(worktree)}},
        )
        before = _store_snapshot()
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 409
        assert _store_snapshot() == before

    def test_story_without_a_worktree_is_409(self, client, plan_dir):
        _write_manifest(plan_dir, PLAN_NAME, {STORY_KEY: {"status": "failed"}})
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 409

    def test_story_whose_worktree_is_not_a_directory_is_409(self, client, plan_dir, tmp_path):
        _write_manifest(
            plan_dir,
            PLAN_NAME,
            {STORY_KEY: {"status": "failed", "worktree": str(tmp_path / "gone")}},
        )
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 409


# =========================================================================== #
# Malformed request bodies
# =========================================================================== #
class TestMalformedBodies:
    @pytest.mark.parametrize("missing", ["plan_name", "story_key", "unified_diff"])
    def test_missing_required_field_is_422(self, client, manifest, missing):
        body = _body()
        body.pop(missing)
        resp = _post(
            client, origin=ORIGIN_CHAT, key=get_or_create_api_key(), body=body
        )
        assert resp.status_code == 422

    def test_non_string_unified_diff_is_422(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=123),
        )
        assert resp.status_code == 422

    def test_malformed_diff_is_413(self, client, manifest):
        # A ``+++`` header with no preceding ``---`` header is a
        # PatchFormatError, which the route maps to 413.
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff="+++ b/x.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n"),
        )
        assert resp.status_code == 413


# =========================================================================== #
# Boundaries: the limits are inclusive, and an empty diff is a no-op patch
# =========================================================================== #
class TestBoundaries:
    def test_exactly_the_byte_limit_is_accepted(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_diff_of_exactly_bytes(65536)),
        )
        assert resp.status_code == 200, resp.text

    def test_one_byte_over_the_limit_is_413(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_diff_of_exactly_bytes(65537)),
        )
        assert resp.status_code == 413

    def test_exactly_five_files_is_accepted(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_n_file_diff(5)),
        )
        assert resp.status_code == 200, resp.text

    def test_exactly_four_hundred_added_lines_is_accepted(self, client, manifest):
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=_new_file_diff(400)),
        )
        assert resp.status_code == 200, resp.text

    def test_empty_diff_is_accepted_as_a_no_op_patch(self, client, manifest):
        # ``validate_for_propose("")`` returns ``{"paths": [], "added_lines": 0}``
        # (an empty diff is not malformed), so the route stores a record with
        # no touched paths rather than refusing.
        resp = _post(
            client,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body=_body(unified_diff=""),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["paths"] == []
        assert body["added_lines"] == 0
        assert worktree_patch.get_patch_record(body["patch_id"]) is not None


# =========================================================================== #
# Chat tool registry (membership only — TOOLS is cumulative)
# =========================================================================== #
class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingHttpClient:
    """Records every ``.get``/``.post`` call and returns a scripted payload."""

    def __init__(self, payload=None) -> None:
        self.calls: list[dict] = []
        self._payload = {} if payload is None else payload

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return _FakeResponse(self._payload)

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return _FakeResponse(self._payload)


class TestChatTool:
    def test_propose_patch_is_registered(self):
        assert "propose_patch" in chat_module.TOOLS

    def test_entry_has_the_required_keys(self):
        entry = chat_module.TOOLS["propose_patch"]
        for key in ("description", "params", "execute"):
            assert key in entry, f"TOOLS['propose_patch'] is missing {key!r}"

    def test_description_is_a_nonempty_string(self):
        description = chat_module.TOOLS["propose_patch"]["description"]
        assert isinstance(description, str)
        assert description.strip() != ""

    def test_params_are_the_three_strings(self):
        assert chat_module.TOOLS["propose_patch"]["params"] == {
            "plan_name": "str",
            "story_key": "str",
            "unified_diff": "str",
        }

    def test_execute_is_callable(self):
        assert callable(chat_module.TOOLS["propose_patch"]["execute"])

    def test_execute_posts_the_exact_body_to_the_propose_route(self):
        client = _RecordingHttpClient(payload={"ok": True, "patch_id": "wp-x"})
        result = chat_module.TOOLS["propose_patch"]["execute"](
            client,
            "http://testserver",
            plan_name="p",
            story_key="S1",
            unified_diff="DIFF",
        )
        assert client.calls, "expected the tool to issue an HTTP call"
        call = client.calls[-1]
        assert call["method"] == "POST"
        assert call["url"].endswith(PROPOSE_PATH)
        assert call.get("json") == {
            "plan_name": "p",
            "story_key": "S1",
            "unified_diff": "DIFF",
        }
        assert result == {"ok": True, "patch_id": "wp-x"}

    def test_tools_sentence_mentions_propose_patch(self):
        sentence = chat_module._available_tools_sentence()
        assert "propose_patch" in sentence


# =========================================================================== #
# REVIEW ROUND 2 (BLOCKING): the confirmation token must never reach chat
# =========================================================================== #
# ``propose_worktree_patch_route`` is chat-reachable BY DESIGN and
# ``app/chat.py``'s ``propose_patch`` tool returns the route's ``.json()``
# straight back to the model.  ``create_patch_record``'s envelope carries
# ``confirmation_token`` -- the credential the dashboard needs to render the
# human-confirmation step -- so returning that envelope verbatim hands the
# constrained party the control's own credential.  A prompt-injected model
# could then satisfy the human-confirmation step itself once the apply route
# lands, defeating the human-in-the-loop control this story exists to build.
#
# Required behaviour pinned below: the token is minted/returned ONLY for
# ``ORIGIN_UI``; a chat-origin propose returns a token-free envelope while
# still creating the record, so the human can retrieve and confirm it.


def _expected_confirmation_token(patch_id: str, diff_hash: str) -> str:
    """The HMAC the store mints for *patch_id*/*diff_hash* (WAP-6 shape)."""
    import hashlib
    import hmac

    return hmac.new(
        worktree_patch._TOKEN_SECRET.encode("utf-8"),
        f"{patch_id}:{diff_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()


class TestChatOriginNeverReceivesTheConfirmationToken:
    def test_chat_origin_response_omits_the_confirmation_token_key(
        self, client, manifest
    ):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "confirmation_token" not in body, (
            "chat origin must not receive the human-confirmation credential"
        )

    def test_chat_origin_response_leaks_no_token_value_under_any_key(
        self, client, manifest
    ):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        token = _expected_confirmation_token(body["patch_id"], body["diff_hash"])
        assert token not in resp.text, (
            "the confirmation token value must not appear anywhere in the "
            "chat-origin response body"
        )

    def test_chat_origin_still_creates_the_record_for_the_human(
        self, client, manifest
    ):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        record = worktree_patch.get_patch_record(body["patch_id"])
        assert record is not None, "the record must still be created for the human"
        assert record["diff_text"] == VALID_DIFF
        assert record["status"] == "pending"
        assert record["diff_hash"] == body["diff_hash"]

    def test_chat_origin_envelope_keeps_the_review_fields(self, client, manifest):
        resp = _post(client, origin=ORIGIN_CHAT, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["paths"] == ["src/app.py"]
        assert body["added_lines"] == 1
        assert body["diff_hash"]

    def test_ui_origin_still_receives_the_real_confirmation_token(
        self, client, manifest
    ):
        resp = _post(client, origin=ORIGIN_UI, key=get_or_create_api_key())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "confirmation_token" in body, (
            "the dashboard (ui origin) needs the token to render the "
            "confirmation step"
        )
        assert body["confirmation_token"] == _expected_confirmation_token(
            body["patch_id"], body["diff_hash"]
        )

    def test_chat_tool_path_returns_no_token_to_the_model(self, client, manifest):
        # The real model-visible path: app/chat.py's propose_patch tool posts
        # with the ORIGIN_CHAT stamp and returns ``.json()`` verbatim.
        client.headers[ORIGIN_HEADER] = ORIGIN_CHAT
        result = chat_module.TOOLS["propose_patch"]["execute"](
            client,
            "",
            plan_name=PLAN_NAME,
            story_key=STORY_KEY,
            unified_diff=VALID_DIFF,
        )
        assert result["ok"] is True
        assert "confirmation_token" not in result, (
            "the model must not receive the confirmation token for its own patch"
        )
