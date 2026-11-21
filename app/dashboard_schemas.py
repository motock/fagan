"""Response schemas for the dashboard HTTP API (story 8ba7da36).

These models are attached to route decorators through
``responses={200: {'model': ...}}``. They document the API contract in the
OpenAPI spec only: FastAPI does not validate or re-serialise the handler's
return value against a ``responses`` model (that is ``response_model=``, which
would filter and reshape it), so adding a schema here cannot change a runtime
response.

Every model sets ``extra='allow'``, so a field the model does not declare is
documented as permitted rather than rejected, and a key is declared *required*
only when every code path of its handler returns it. The shapes were read from
the handlers and the ``pipeline.service`` methods they delegate to; where a
payload is genuinely free-form (the worktree diff summary, an SSE frame) the
schema says so instead of inventing fields.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class WorkspaceEntry(BaseModel):
    """One row of ``GET /api/workspaces`` (``list_workspaces`` entry)."""

    model_config = ConfigDict(extra="allow")

    path: str
    exists: bool
    valid: bool


class WorkspacesResponse(BaseModel):
    """Response of ``GET /api/workspaces``."""

    model_config = ConfigDict(extra="allow")

    workspaces: list[WorkspaceEntry]


class ActiveWorkspaceResponse(BaseModel):
    """Response of ``GET /api/workspace``: the active path, or null."""

    model_config = ConfigDict(extra="allow")

    active: str | None


class WorkspaceResolveResponse(BaseModel):
    """Response of ``POST /api/workspace`` (``validate/create_workspace``).

    ``pipeline.workspace.validate_workspace`` and ``create_workspace`` always
    return ``ok``/``path``/``error``; the route answers 400 instead of 200 when
    ``ok`` is false, so a 200 always carries ``ok: true`` plus the resolved path.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    path: str
    error: str | None


class WorkspaceFileResponse(BaseModel):
    """Response of ``GET /api/workspace/file`` (``read_workspace_file``).

    ``content`` is the file's UTF-8 text, so it is free-form text rather than a
    structured object.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    path: str
    content: str


class DirectoryEntry(BaseModel):
    """One entry of ``GET /api/workspace/files``."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str


class WorkspaceEntriesResponse(BaseModel):
    """Response of ``GET /api/workspace/files`` (``list_workspace_directory``)."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    path: str
    entries: list[DirectoryEntry]


class WorkspaceSearchResponse(BaseModel):
    """Response of ``GET /api/workspace/search`` (``search_workspace``).

    ``matches`` are raw ``grep`` output lines; ``truncated`` says whether the
    result was cut at the service's ``max_results`` ceiling.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    matches: list[str]
    truncated: bool


class PatchReviewResponse(BaseModel):
    """Response of ``GET /api/worktree/patch/{patch_id}``.

    Every key below is returned unconditionally by the handler. ``diff_text``
    is the stored unified diff (free-form text) and ``confirmation_token`` is
    the apply credential the trusted UI renders for human confirmation.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    patch_id: str
    plan_name: str
    story_key: str
    paths: list[str]
    added_lines: int
    diff_text: str
    status: str
    created_at: str
    expires_at: str
    confirmation_token: str


class PatchApplyResponse(BaseModel):
    """Response of ``POST /api/worktree/patch/{patch_id}/apply``.

    A refused apply raises HTTPException with the engine's ``status_code``, so a
    200 always carries ``ok: true``; the rest of the engine result is
    engine-defined and therefore documented as allowed but not required.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool


class SavePlanResponse(BaseModel):
    """Response of ``POST /api/plans/{plan_name}/save`` (``save_plan``)."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    path: str
    epic_count: int
    story_count: int


class DecomposeResponse(BaseModel):
    """Response of ``POST /api/decompose`` (``decompose_plan``).

    ``plan`` is the model-authored plan object (free-form by nature).
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    plan: dict[str, Any]


class IngestPlanResponse(BaseModel):
    """Response of ``POST /api/plans/{plan_name}/ingest`` (``ingest_plan``).

    A failed ingest raises HTTPException with the service's error, so a 200
    always carries ``ok: true``; the remaining keys come from the ingest
    implementation and are documented as allowed, not required.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool


class StoryDiffFile(BaseModel):
    """One entry of a story diff summary's ``files`` list."""

    model_config = ConfigDict(extra="allow")

    path: str
    status: str = ""
    added: int = 0
    removed: int = 0
    binary: bool = False
    untracked: bool = False


class StoryDiffResponse(BaseModel):
    """Response of ``GET /api/plans/{plan_name}/stories/{story_key}/diff``.

    The route returns one of three shapes -- ``{"available": false, "reason":
    ...}``, a summary (``files``/``totals``/``truncated``/shas), or a single
    file's diff (``path``/``diff``/``binary``) -- so ``available`` is the only
    key every branch returns. The rest is declared as optional, and the
    ``diff`` body itself is free-form text.
    """

    model_config = ConfigDict(extra="allow")

    available: bool
    reason: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    files: list[StoryDiffFile] = []
    totals: dict[str, int] | None = None
    truncated: bool = False
    path: str | None = None
    diff: str | None = None
    binary: bool | None = None
