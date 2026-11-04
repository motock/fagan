"""Tests for static/app/render/decisions.js — the "Overlord decisions" panel.

The same log holds two record shapes: real overlord rulings (story_key,
question, ruling, rationale, decided_by, decided_at) and plan-approval records
(at, decided_by, role, status, digest, self_approved, correlation_id, plan,
warnings_seen — no ``question``). Each shape must render without printing
``undefined``, ``null`` or a placeholder for a missing field.
"""
import json
import os

from tests.unit._app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DECISIONS_JS = os.path.join(REPO_ROOT, "static", "app", "render", "decisions.js")
SHIM = "globalThis.window=globalThis;globalThis.document={getElementById:()=>null};"

APPROVAL = {
    "at": "2026-11-04T12:19:50+00:00",
    "decided_by": "https://idp.example.test/realms/p#abc",
    "role": "operator",
    "status": "approved",
    "digest": "d1",
    "self_approved": True,
    "plan": "p",
    "correlation_id": "c1",
    "warnings_seen": [],
}

OVERLORD = {
    "story_key": "S-1",
    "question": "high-risk merge adjudication",
    "ruling": "approve",
    "rationale": "r",
    "decided_by": "overlord",
    "decided_at": "2026-11-04T01:00:00+00:00",
}


def _render(records):
    proc = run_app_js(
        f"JSON.stringify(renderDecisions({json.dumps(records)}))",
        app_js=DECISIONS_JS,
        shim=SHIM,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_plan_approval_renders_plan_status_title():
    html = _render([APPROVAL])
    assert "Plan approved" in html


def test_plan_approval_renders_no_undefined():
    html = _render([APPROVAL])
    assert "undefined" not in html


def test_plan_approval_renders_no_tier_placeholder():
    html = _render([APPROVAL])
    assert "tier=" not in html


def test_plan_approval_renders_actor_and_timestamp():
    html = _render([APPROVAL])
    assert "by operator (self-approved)" in html
    assert "2026-11-04T12:19:50+00:00" in html


def test_plan_approval_without_self_approval_omits_marker():
    html = _render([dict(APPROVAL, self_approved=False)])
    assert "self-approved" not in html


def test_plan_approval_rejected_renders_rejected_status():
    html = _render([dict(APPROVAL, status="rejected")])
    assert "Plan rejected" in html


def test_plan_approval_without_role_falls_back_to_decided_by():
    record = dict(APPROVAL, self_approved=False)
    record.pop("role")
    html = _render([record])
    assert "by https://idp.example.test/realms/p#abc" in html


def test_overlord_decision_renders_question_and_ruling():
    html = _render([OVERLORD])
    assert "high-risk merge adjudication" in html
    assert "approve" in html
    assert "tier=?" not in html
    assert "undefined" not in html


def test_overlord_decision_renders_tier_and_risk_when_present():
    html = _render([dict(OVERLORD, tier="t1", risk="low")])
    assert "tier=t1" in html
    assert "risk=low" in html


def test_empty_list_renders_empty_state():
    html = _render([])
    assert "No decisions logged yet." in html


def test_empty_record_renders_neutral_title():
    html = _render([{}])
    assert "Decision recorded" in html
    assert "undefined" not in html


def test_non_string_question_and_null_status_render_neutral_title():
    html = _render([{"question": 5, "status": None}])
    assert "Decision recorded" in html
    assert "undefined" not in html
    assert "null" not in html


def test_question_is_html_escaped():
    html = _render([dict(OVERLORD, question="<script>alert(1)</script>")])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_newest_record_is_rendered_first():
    html = _render([OVERLORD, APPROVAL])
    assert html.index("Plan approved") < html.index("high-risk merge adjudication")
