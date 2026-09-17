"""Regression test for POST /api/plans/{plan_name}/decisions.

This is the plan-level decision route `add_decision` (app/dashboard.py) that
the chat app's `answer_decision` tool calls to record a human override in a
plan's decision log (see app/chat.py's TOOLS["answer_decision"]).

Found live 2026-09-17: the handler called `_service.append_decision(...)`,
but `PipelineService` never defined that method (only `pipeline.store`'s
FileStore did). Every call 500'd with an AttributeError, and the response
body was plain text ("Internal Server Error"), not JSON -- which is exactly
why the chat tool's `.json()` call failed with a parse error and no human
decision was ever recorded, despite the chat session reporting success.

Deliberately does NOT mock `_service` (unlike test_dashboard_decision_route.py's
story-level route tests) -- mocking it here would hide the exact bug this
guards against, since the AttributeError lives inside PipelineService itself,
not in how the route calls it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


def test_add_decision_returns_200_not_500(client, plan_dir):
    res = client.post(
        "/api/plans/someplan/decisions",
        json={
            "story_key": "S1",
            "question": "Which model should this story dispatch on?",
            "answer": "Override to gpt-oss-20b-high:latest",
            "context": "devstral:24b 404s on this host",
        },
    )

    assert res.status_code == 200, (
        f"expected 200, got {res.status_code} with body {res.text!r} "
        "(a 500 here reproduces the PipelineService.append_decision "
        "AttributeError)"
    )


def test_add_decision_response_is_json_with_ok_true(client, plan_dir):
    res = client.post(
        "/api/plans/someplan/decisions",
        json={
            "story_key": "S1",
            "question": "Q",
            "answer": "A",
        },
    )

    body = res.json()
    assert body["ok"] is True


def test_add_decision_persists_to_the_plan_decision_log(client, plan_dir):
    client.post(
        "/api/plans/someplan/decisions",
        json={
            "story_key": "S1",
            "question": "Which model?",
            "answer": "gpt-oss-20b-high:latest",
            "context": "human override",
        },
    )

    decisions = d._service.get_decisions("someplan")
    assert any(
        rec.get("decision") == "gpt-oss-20b-high:latest"
        and rec.get("story_key") == "S1"
        for rec in decisions
    ), f"human decision was not persisted; got {decisions!r}"


def test_add_decision_defaults_decided_by_to_human(client, plan_dir):
    client.post(
        "/api/plans/someplan/decisions",
        json={"story_key": "S1", "question": "Q", "answer": "A"},
    )

    decisions = d._service.get_decisions("someplan")
    assert decisions[-1]["decided_by"] == "human"
