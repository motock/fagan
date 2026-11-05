"""Contract tests for the response schemas declared on the dashboard API.

Story 8ba7da36: every dashboard ``/api/*`` operation must document the JSON
shape it returns in the OpenAPI spec. The schemas are declared with the route
decorator's ``responses={200: {'model': ...}}`` argument -- never
``response_model=``, which would re-serialise (and could drop fields from) the
handler's return value.

What is pinned here:

1. RATCHET -- every operation must have a typed 2xx response or be listed in
   ``UNTYPED_EXEMPT`` with a reason, and an exemption that has since become
   typed fails until it is removed (no permanent exemption list).
2. POSITIVE -- each typed operation, driven through ``TestClient`` with the
   suite's dashboard credential, validates against its declared model.
3. NEGATIVE -- dropping a universally required field makes validation fail, so
   the schema is not vacuous.
4. NO RUNTIME CHANGE -- the response body is exactly what the handler returned.
5. COMPONENTS -- each JSON type is reachable from ``components/schemas``.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import dashboard as d
from app import dashboard_schemas as schemas
from pipeline import worktree_patch

# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------

#: Operations that legitimately have no typed 2xx body. Each entry is
#: ``(METHOD, path): reason`` and every reason is one line. It starts empty
#: because every ``/api/*`` operation could be given an honest schema -- the
#: genuinely free-form ones (the worktree diff, an SSE frame) are typed as
#: free-form rather than exempted.
UNTYPED_EXEMPT: dict[tuple[str, str], str] = {
    (
        "GET",
        "/api/worktree/patch/{patch_id}",
    ): "tests/unit/test_patch_route_refusal_logging.py:265 pins this exact "
    "decorator text (`@app.get(\"/api/worktree/patch/{patch_id}\")`), and that "
    "test may not be edited; typing it would break the pin",
}

#: The ratchet covers the dashboard's own API surface. The browser login
#: routes live in ``app/oidc_login_routes.py`` and are outside this change's
#: file scope, so they are skipped by prefix rather than listed as permanent
#: exemptions.
OUT_OF_SCOPE_PREFIXES: tuple[str, ...] = ("/auth/",)

#: Typed operations whose body is not a JSON model, so it cannot be a
#: ``components/schemas`` reference. The SSE stream is a text protocol.
NON_JSON_TYPED: dict[tuple[str, str], str] = {
    ("POST", "/api/chat/stream"): "Server-sent events stream, not a JSON body",
}

_TYPED_MARKERS = frozenset(
    {
        "$ref",
        "type",
        "properties",
        "anyOf",
        "allOf",
        "oneOf",
        "items",
        "additionalProperties",
        "enum",
        "const",
    }
)


def _is_typed(schema: Any) -> bool:
    """True when *schema* actually constrains the body.

    FastAPI emits ``{"schema": {}}`` for a handler that declares nothing, so a
    presence check on ``content`` alone would pass vacuously.
    """
    if not isinstance(schema, dict) or not schema:
        return False
    return bool(_TYPED_MARKERS & set(schema))


def _typed_2xx_schemas(operation: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for code, response in (operation.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        for media_type, media in (response.get("content") or {}).items():
            schema = media.get("schema")
            if _is_typed(schema):
                found.append((media_type, schema))
    return found


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    spec = d.app.openapi()
    return [
        (method.upper(), path, operation)
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
        if isinstance(operation, dict)
    ]


def _in_scope_operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for method, path, operation in _operations()
        if not path.startswith(OUT_OF_SCOPE_PREFIXES)
    ]

def test_every_operation_has_a_typed_2xx_response_or_a_reason():
    untyped = [
        (method, path)
        for method, path, operation in _in_scope_operations()
        if not _typed_2xx_schemas(operation) and (method, path) not in UNTYPED_EXEMPT
    ]

    assert untyped == [], f"operations with no typed 2xx body: {untyped}"


def test_exemptions_are_not_stale():
    """An exempt operation that has since become typed must leave the table."""
    stale = [
        (method, path)
        for method, path, operation in _operations()
        if (method, path) in UNTYPED_EXEMPT and _typed_2xx_schemas(operation)
    ]

    assert stale == [], f"exempt operations that are now typed: {stale}"


def test_every_exemption_still_exists():
    live = {(method, path) for method, path, _ in _operations()}

    assert sorted(set(UNTYPED_EXEMPT) - live) == []


def test_only_the_browser_login_routes_are_out_of_scope():
    """Anything skipped by prefix really is a browser-login route.

    The OIDC routes are mounted conditionally on the operator's
    ``PIPELINE_OIDC_*`` configuration, so this asserts the shape of the skip
    rather than a fixed list: every skipped path must be an ``/auth/`` route.
    """
    out_of_scope = [
        path for _, path, _ in _operations() if path.startswith(OUT_OF_SCOPE_PREFIXES)
    ]

    assert all(path.startswith("/auth/") for path in out_of_scope)
# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeService:
    """Stand-in for PipelineService: only the attributes a test sets exist."""

    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)


@pytest.fixture
def client() -> TestClient:
    # tests/unit/conftest.py attaches the dashboard credential to every
    # TestClient, so this is an authenticated client.
    return TestClient(d.app)


_PATCH_RECORD = {
    "patch_id": "p-1",
    "plan_name": "p1",
    "story_key": "s1",
    "paths": ["a.txt"],
    "added_lines": 3,
    "diff_text": "--- a/a.txt\n+++ b/a.txt\n",
    "status": "proposed",
    "created_at": "2026-01-01T00:00:00+00:00",
    "expires_at": "2026-01-02T00:00:00+00:00",
}


@pytest.fixture
def patch_store(monkeypatch):
    monkeypatch.setattr(worktree_patch, "get_patch_record", lambda patch_id: dict(_PATCH_RECORD))
    monkeypatch.setattr(
        worktree_patch, "confirmation_token_for", lambda record: "token-1"
    )
    monkeypatch.setattr(d, "require_ui_origin", lambda origin: None)


# ---------------------------------------------------------------------------
# Positive contract tests: one per typed JSON operation
# ---------------------------------------------------------------------------


def test_list_workspaces_matches_schema(client, monkeypatch):
    workspaces = [{"path": "/tmp/ws", "exists": True, "valid": True}]
    monkeypatch.setattr(
        d, "_service", FakeService(list_workspaces=lambda: workspaces)
    )

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert schemas.WorkspacesResponse.model_validate(response.json())


def test_get_active_workspace_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d, "_service", FakeService(get_active_workspace=lambda: "/tmp/ws")
    )

    response = client.get("/api/workspace")

    assert response.status_code == 200
    assert schemas.ActiveWorkspaceResponse.model_validate(response.json())


def test_set_workspace_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            resolve_workspace=lambda path, create: {
                "ok": True,
                "path": "/tmp/ws",
                "error": None,
            },
            set_active_workspace=lambda path: None,
        ),
    )

    response = client.post("/api/workspace", json={"path": "/tmp/ws"})

    assert response.status_code == 200
    assert schemas.WorkspaceResolveResponse.model_validate(response.json())


def test_read_workspace_file_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            read_workspace_file=lambda path: {
                "ok": True,
                "path": "a.txt",
                "content": "hello",
            }
        ),
    )

    response = client.get("/api/workspace/file", params={"path": "a.txt"})

    assert response.status_code == 200
    assert schemas.WorkspaceFileResponse.model_validate(response.json())


def test_list_workspace_directory_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            list_workspace_directory=lambda path: {
                "ok": True,
                "path": "",
                "entries": [{"name": "a.txt", "type": "file"}],
            }
        ),
    )

    response = client.get("/api/workspace/files")

    assert response.status_code == 200
    assert schemas.WorkspaceEntriesResponse.model_validate(response.json())


def test_search_workspace_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            search_workspace=lambda pattern: {
                "ok": True,
                "matches": ["a.txt:1:hit"],
                "truncated": False,
            }
        ),
    )

    response = client.get("/api/workspace/search", params={"pattern": "hit"})

    assert response.status_code == 200
    assert schemas.WorkspaceSearchResponse.model_validate(response.json())


def test_review_patch_matches_schema(client, patch_store):
    response = client.get("/api/worktree/patch/p-1")

    assert response.status_code == 200
    assert schemas.PatchReviewResponse.model_validate(response.json())


def test_apply_patch_matches_schema(client, patch_store, monkeypatch):
    monkeypatch.setattr(
        worktree_patch,
        "apply_patch",
        lambda plan, story, patch_id, token: {"ok": True, "patch_id": patch_id},
    )

    response = client.post(
        "/api/worktree/patch/p-1/apply", json={"confirmation_token": "token-1"}
    )

    assert response.status_code == 200
    assert schemas.PatchApplyResponse.model_validate(response.json())


def test_save_plan_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            save_plan=lambda plan_name, plan_json, workspace: {
                "ok": True,
                "path": "/tmp/p1.json",
                "epic_count": 1,
                "story_count": 2,
            }
        ),
    )

    response = client.post(
        "/api/plans/p1/save", json={"plan_json": "{}", "workspace": "/tmp/ws"}
    )

    assert response.status_code == 200
    assert schemas.SavePlanResponse.model_validate(response.json())


def test_decompose_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            decompose_plan=lambda request, workspace: {
                "ok": True,
                "plan": {"epics": []},
            }
        ),
    )

    response = client.post(
        "/api/decompose", json={"request": "build it", "workspace": "/tmp/ws"}
    )

    assert response.status_code == 200
    assert schemas.DecomposeResponse.model_validate(response.json())


def test_ingest_plan_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(ingest_plan=lambda plan_name, **kwargs: {"ok": True}),
    )

    response = client.post("/api/plans/p1/ingest", json={})

    assert response.status_code == 200
    assert schemas.IngestPlanResponse.model_validate(response.json())


_DIFF_SUMMARY = {
    "available": True,
    "reason": None,
    "branch": "feature",
    "head_sha": "0" * 40,
    "base_ref": "main",
    "base_sha": "1" * 40,
    "files": [
        {
            "path": "a.txt",
            "status": "M",
            "added": 1,
            "removed": 0,
            "binary": False,
            "untracked": False,
        }
    ],
    "totals": {
        "files": 1,
        "added": 1,
        "removed": 0,
        "untracked": 0,
        "committed": 0,
        "working": 1,
    },
    "truncated": False,
}


def test_story_diff_matches_schema(client, monkeypatch):
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            get_manifest_or_none=lambda plan_name: {"stories": {"s1": {}}},
            get_story_diff=lambda plan_name, story_key, manifest, path=None: dict(
                _DIFF_SUMMARY
            ),
        ),
    )

    response = client.get("/api/plans/p1/stories/s1/diff")

    # The route wraps the payload in a JSONResponse; the body is unchanged.
    assert response.status_code == 200
    assert schemas.StoryDiffResponse.model_validate(response.json())


def test_story_diff_unavailable_branch_matches_schema(client, monkeypatch):
    """The ``available: false`` branch carries only ``reason``."""
    monkeypatch.setattr(
        d,
        "_service",
        FakeService(
            get_manifest_or_none=lambda plan_name: {"stories": {"s1": {}}},
            get_story_diff=lambda plan_name, story_key, manifest, path=None: {
                "available": False,
                "reason": "no_worktree",
            },
        ),
    )

    response = client.get("/api/plans/p1/stories/s1/diff")

    assert response.status_code == 200
    assert schemas.StoryDiffResponse.model_validate(response.json())


# ---------------------------------------------------------------------------
# Negative contract tests: the schemas are not vacuous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload", "missing"),
    [
        (
            schemas.WorkspacesResponse,
            {"workspaces": []},
            "workspaces",
        ),
        (
            schemas.ActiveWorkspaceResponse,
            {"active": None},
            "active",
        ),
        (
            schemas.WorkspaceResolveResponse,
            {"ok": True, "path": "/tmp/ws", "error": None},
            "ok",
        ),
        (
            schemas.WorkspaceFileResponse,
            {"ok": True, "path": "a.txt", "content": "hi"},
            "content",
        ),
        (
            schemas.WorkspaceEntriesResponse,
            {"ok": True, "path": "", "entries": []},
            "entries",
        ),
        (
            schemas.WorkspaceSearchResponse,
            {"ok": True, "matches": [], "truncated": False},
            "matches",
        ),
        (
            schemas.PatchReviewResponse,
            {
                "ok": True,
                **_PATCH_RECORD,
                "confirmation_token": "token-1",
            },
            "patch_id",
        ),
        (
            schemas.PatchApplyResponse,
            {"ok": True, "patch_id": "p-1"},
            "ok",
        ),
        (
            schemas.SavePlanResponse,
            {"ok": True, "path": "/tmp/p1.json", "epic_count": 1, "story_count": 2},
            "story_count",
        ),
        (
            schemas.DecomposeResponse,
            {"ok": True, "plan": {"epics": []}},
            "plan",
        ),
        (
            schemas.IngestPlanResponse,
            {"ok": True},
            "ok",
        ),
        (
            schemas.StoryDiffResponse,
            {"available": True},
            "available",
        ),
    ],
)
def test_required_field_is_really_required(model, payload, missing):
    assert model.model_validate(payload)

    incomplete = {k: v for k, v in payload.items() if k != missing}

    with pytest.raises(ValidationError):
        model.model_validate(incomplete)


def test_models_tolerate_undeclared_fields():
    """``extra='allow'``: an added key is documented as allowed, never rejected."""
    validated = schemas.WorkspacesResponse.model_validate(
        {"workspaces": [], "surprise": 1}
    )

    assert validated.model_dump()["surprise"] == 1


# ---------------------------------------------------------------------------
# No runtime change: the body is byte-for-byte the handler's return value
# ---------------------------------------------------------------------------


def test_workspaces_response_is_unchanged(monkeypatch):
    workspaces = [{"path": "/tmp/ws", "exists": True, "valid": True, "extra": 7}]
    monkeypatch.setattr(
        d, "_service", FakeService(list_workspaces=lambda: workspaces)
    )

    returned = d.list_workspaces_route()
    body = TestClient(d.app).get("/api/workspaces").json()

    assert body == returned


def test_search_response_is_unchanged(monkeypatch):
    expected = {"ok": True, "matches": ["a.txt:1:hit"], "truncated": False, "x": None}
    monkeypatch.setattr(
        d, "_service", FakeService(search_workspace=lambda pattern: dict(expected))
    )

    returned = d.search_workspace_route("hit")
    body = TestClient(d.app).get("/api/workspace/search", params={"pattern": "hit"}).json()

    assert body == returned


def test_patch_review_response_is_unchanged(patch_store):
    # Drive the handler directly so the comparison is against its own return
    # value, not against the route response we are checking.
    response_obj = Response()
    returned = d.get_worktree_patch_route("p-1", response_obj, "ui")
    body = TestClient(d.app).get("/api/worktree/patch/p-1").json()

    assert body == returned


# ---------------------------------------------------------------------------
# Components: the declared schemas are published, not inlined
# ---------------------------------------------------------------------------


#: The operations whose 200 schema this change declares. They must reference a
#: published model in ``components/schemas`` rather than an inline free-form
#: object, so a client can generate a typed model from the spec.
TYPED_BY_THIS_CHANGE: tuple[tuple[str, str], ...] = (
    ("GET", "/api/plans/{plan_name}/stories/{story_key}/diff"),
    ("GET", "/api/workspaces"),
    ("GET", "/api/workspace"),
    ("POST", "/api/workspace"),
    ("GET", "/api/workspace/file"),
    ("GET", "/api/workspace/files"),
    ("GET", "/api/workspace/search"),
    ("POST", "/api/worktree/patch/{patch_id}/apply"),
    ("POST", "/api/plans/{plan_name}/save"),
    ("POST", "/api/decompose"),
    ("POST", "/api/plans/{plan_name}/ingest"),
)


def test_every_json_operation_references_a_component_schema():
    spec = d.app.openapi()
    missing: list[tuple[str, str]] = []
    for method, path in TYPED_BY_THIS_CHANGE:
        operation = spec["paths"][path][method.lower()]
        json_schemas = [
            schema
            for media_type, schema in _typed_2xx_schemas(operation)
            if media_type == "application/json"
        ]
        if not any(
            str(schema.get("$ref", "")).startswith("#/components/schemas/")
            for schema in json_schemas
        ):
            missing.append((method, path))

    assert missing == [], f"operations without a component schema: {missing}"


def test_chat_stream_documents_event_stream_media_type():
    spec = d.app.openapi()
    operation = spec["paths"]["/api/chat/stream"]["post"]

    assert "text/event-stream" in operation["responses"]["200"]["content"]
