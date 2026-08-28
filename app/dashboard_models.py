"""Pydantic request bodies for app/dashboard.py's FastAPI routes.

Pulled out of dashboard.py (which re-imports and re-exports every name
here) purely to shrink that file; these classes carry no behavior of
their own.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SavePlanRequest(BaseModel):
    plan_json: str


class IngestPlanRequest(BaseModel):
    only_epics: list[str] | None = None
    overwrite: bool = False


class DecomposeRequest(BaseModel):
    request: str


class DecisionRequest(BaseModel):
    story_key: str
    question: str
    answer: str
    context: str = ""
    decided_by: str = "human"


class StoryDecisionRequest(BaseModel):
    question: str
    options: list[str]
    context: str = ""
    decided_by: str = "human"


class RoleDefaultBody(BaseModel):
    provider: str
    model: str


class PlanRoleConfigBody(BaseModel):
    provider: str | None = None
    model: str | None = None


class StoryStatusBody(BaseModel):
    status: str


class StoryPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_instructions: str | None = None
    model: str | None = None
    persona: str | None = None
    risk: str | None = None
    dependencies: str | None = None
    acceptance: str | None = None
    pr_url: str | None = None
    summary: str | None = None
    tdd_split: str | None = None
    backend: str | None = None


class WorkspaceRequest(BaseModel):
    path: str
    create: bool = False
