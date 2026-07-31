"""Acceptance oracle: the escalation paths must carry the diagnosis forward.

_escalate_to_claude wipes the worktree, branch and journal and re-dispatches
with agent_instructions UNCHANGED - a blind retry, which CLAUDE.md Step 9
forbids. Grading the wiring (not just the compose helper) is the point: a
composer nothing calls re-briefs nothing.
"""
import inspect

from pipeline import escalation


def test_escalate_to_claude_rebriefs_the_story():
    src = inspect.getsource(escalation._escalate_to_claude)
    assert "compose_rebriefed_instructions" in src


def test_local_fallback_escalation_rebriefs_the_story():
    src = inspect.getsource(escalation._escalate_to_local_fallback_model)
    assert "compose_rebriefed_instructions" in src


def test_the_rebrief_happens_before_the_worktree_is_removed():
    src = inspect.getsource(escalation._escalate_to_claude)
    rebrief = src.index("compose_rebriefed_instructions")
    removal = src.index('"worktree", "remove"')
    assert rebrief < removal, (
        "evidence must be collected from the worktree BEFORE it is deleted"
    )


def test_story_instructions_are_updated_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "collect_failure_evidence", lambda *a, **k: "evidence")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: "root cause here")
    monkeypatch.setattr(escalation.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "_atomic_write_json", lambda *a, **k: None)

    manifest = {"stories": {"S1": {"agent_instructions": "GOAL: x", "worktree": str(tmp_path)}}}
    escalation._escalate_to_claude(manifest, "plan", "S1", tmp_path / "m.json")
    assert "root cause here" in manifest["stories"]["S1"]["agent_instructions"]


def test_failed_diagnosis_leaves_instructions_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "collect_failure_evidence", lambda *a, **k: "evidence")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: None)
    monkeypatch.setattr(escalation.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "_atomic_write_json", lambda *a, **k: None)

    manifest = {"stories": {"S1": {"agent_instructions": "GOAL: x", "worktree": str(tmp_path)}}}
    escalation._escalate_to_claude(manifest, "plan", "S1", tmp_path / "m.json")
    assert manifest["stories"]["S1"]["agent_instructions"] == "GOAL: x"
