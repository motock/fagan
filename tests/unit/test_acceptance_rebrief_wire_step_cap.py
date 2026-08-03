"""Acceptance oracle: the routine step-cap resume must carry a diagnosis forward.

check_story_status's step-cap branch checkpoints an interrupted run with an empty
next_hint; the resume then seeds only a generic "continue from your WIP" hint
(persona._build_dispatch_command), never an analysis of where the implementer
struggled — a blind retry, which CLAUDE.md Step 9 forbids. Grading the wiring
(not just the compose helper) is the point: a composer nothing calls re-briefs
nothing. Mirrors test_acceptance_rebrief_wire_escalation.py.
"""
import inspect

from pipeline import server as p


def test_step_cap_helper_rebriefs_the_story():
    src = inspect.getsource(p._rebrief_step_cap_struggle)
    assert "compose_rebriefed_instructions" in src


def test_check_story_status_calls_step_cap_rebrief():
    src = inspect.getsource(p.check_story_status)
    assert "_rebrief_step_cap_struggle" in src


def test_step_cap_rebrief_folds_diagnosis_into_instructions(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "collect_failure_evidence", lambda *a, **k: "evidence")
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: "root cause here")

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path))
    assert "root cause here" in story["agent_instructions"]
    # The original brief is preserved (compose appends, it does not replace it).
    assert "GOAL: x" in story["agent_instructions"]


def test_failed_step_cap_diagnosis_leaves_instructions_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "collect_failure_evidence", lambda *a, **k: "evidence")
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path))
    assert story["agent_instructions"] == "GOAL: x"