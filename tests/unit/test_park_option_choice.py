"""Choosing a park option: the human half of park options."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import pipeline.server as p
from app import dashboard as d
from pipeline import park_option_choice as poc
from pipeline.park_options import _digest

PLAN = "pc1"
REASON = "review rejected"


def _options(reason=REASON, recommended="o2"):
    return {
        "kind": "park_options",
        "story_key": "s1",
        "park_class": "scope_gate",
        "parked_reason_digest": _digest(reason),
        "options": [
            {"id": "o1", "action": "amend_scope", "title": "Add file", "rationale": "guard names it", "diagnosis": None},
            {"id": "o2", "action": "retry", "title": "Retry", "rationale": "may pass", "diagnosis": None},
        ],
        "recommended": recommended,
        "decided_by": "overlord",
        "decided_at": "2026-11-20T00:00:00+00:00",
    }


@pytest.fixture
def plan(plan_dir):
    def write(story=None):
        story = story if story is not None else {
            "status": "parked", "parked_reason": REASON, "rework_attempts": 3, "triage_attempts": 2,
        }
        (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps({"stories": {"s1": story}}))

    return write


def _story():
    return p._store.get_manifest(PLAN)["stories"]["s1"]


def _log(kind=None, question=None):
    return [
        r for r in p._store.get_decisions(PLAN)
        if (kind and r.get("kind") == kind) or (question and r.get("question") == question)
    ]


def test_retry_interrupts_story_clears_counters_and_records_one_resolution(plan):
    plan()
    p._store.append_decision(PLAN, _options())
    result = poc.choose_park_option(PLAN, "s1", "o2")
    assert result["ok"] is True
    story = _story()
    assert story["status"] == "interrupted"
    assert "rework_attempts" not in story and "triage_attempts" not in story
    resolutions = _log(question="human resolution")
    assert [r["human_action"] for r in resolutions] == ["retry"]
    chosen = _log(kind="park_option_chosen")
    assert chosen[0]["chose_recommended"] is True and chosen[0]["recommended"] == "o2"


def test_amend_scope_runs_the_ruling_and_records_not_recommended(plan, monkeypatch):
    plan()
    p._store.append_decision(PLAN, _options())
    seen = []

    def fake(plan_name, story_key, story, ruling, manifest):
        seen.append(ruling)
        story["status"] = "interrupted"
        return "amend_scope"

    monkeypatch.setattr(poc, "execute_ruling", fake)
    assert poc.choose_park_option(PLAN, "s1", "o1")["ok"] is True
    assert seen == [{
        "action": "amend_scope", "park_class": "scope_gate", "rationale": "guard names it",
        "diagnosis": None, "decided_by": "human",
    }]
    assert _story()["status"] == "interrupted"
    chosen = _log(kind="park_option_chosen")[0]
    assert chosen["chose_recommended"] is False and chosen["action"] == "amend_scope"
    assert _log(question="human resolution")[0]["human_action"] == "amend_scope"


@pytest.mark.parametrize(
    ("story", "records", "option", "code"),
    [
        ({"status": "todo"}, [_options()], "o1", "not_parked"),
        ({"status": "parked", "parked_reason": REASON}, [], "o1", "no_options"),
        ({"status": "parked", "parked_reason": "changed"}, [_options()], "o1", "options_stale"),
        ({"status": "parked", "parked_reason": REASON}, [_options()], "o9", "unknown_option"),
    ],
)
def test_refusals_carry_their_code_and_change_nothing(plan, story, records, option, code):
    plan(story)
    for record in records:
        p._store.append_decision(PLAN, record)
    before = p._store.get_manifest(PLAN)
    decisions = len(p._store.get_decisions(PLAN))
    assert poc.choose_park_option(PLAN, "s1", option) == {"ok": False, "error": code}
    assert p._store.get_manifest(PLAN) == before
    assert len(p._store.get_decisions(PLAN)) == decisions


@pytest.fixture
def client():
    return TestClient(d.app)


URL = f"/api/plans/{PLAN}/stories/s1/park-options"


def test_post_is_403_when_authorize_refuses(plan, client, monkeypatch):
    plan()
    p._store.append_decision(PLAN, _options())
    monkeypatch.setattr("pipeline.authorization.authorize", lambda *a, **k: {"ok": False, "error": "nope bob"})
    response = client.post(f"{URL}/o2/choose")
    assert response.status_code == 403
    assert "bob" not in response.text and "decision:answer" in response.text
    assert _story()["status"] == "parked"


def test_post_is_409_with_the_code_on_refusal(plan, client):
    plan()
    p._store.append_decision(PLAN, _options())
    response = client.post(f"{URL}/o9/choose")
    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "unknown_option"}


def test_post_is_404_for_an_unknown_story(plan, client):
    plan()
    assert client.post(f"/api/plans/{PLAN}/stories/nope/park-options/o1/choose").status_code == 404


def test_post_retry_succeeds(plan, client):
    plan()
    p._store.append_decision(PLAN, _options())
    response = client.post(f"{URL}/o2/choose")
    assert response.status_code == 200 and response.json()["new_status"] == "interrupted"


def test_get_is_preparing_before_options_exist(plan, client):
    plan()
    assert client.get(URL).json() == {"ok": True, "options": [], "preparing": True}


def test_get_reports_options_then_stale_after_the_reason_changes(plan, client):
    plan()
    p._store.append_decision(PLAN, _options())
    body = client.get(URL).json()
    assert body["stale"] is False and body["recommended"] == "o2" and len(body["options"]) == 2
    assert body["park_class"] == "scope_gate"
    plan({"status": "parked", "parked_reason": "changed"})
    assert client.get(URL).json()["stale"] is True


def test_get_is_404_for_an_unknown_plan(client):
    assert client.get("/api/plans/nope-plan/stories/s1/park-options").status_code == 404
