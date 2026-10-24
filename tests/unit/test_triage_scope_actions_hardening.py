"""Hardening tests for the amend_scope guardrail (root files, protected modules, control chars)."""

import pytest

from pipeline import triage_scope_actions as tsa
from pipeline.triage_scope_actions import (
    PROTECTED_MODULES,
    _execute_amend_scope,
    check_scope_amendment,
)

PROTECTED = [
    "pipeline/scope_gate.py",
    "pipeline/scope_conflict.py",
    "pipeline/no_progress.py",
    "pipeline/branch_guard.py",
    "pipeline/triage.py",
    "pipeline/triage_scope_actions.py",
    "pipeline/overlord.py",
    "pipeline/review_orchestrator.py",
    "pipeline/merge.py",
    "pipeline/advance_merge.py",
    "pipeline/dispatch_lease.py",
    "pipeline/pin_guard.py",
    "pipeline/security_audit.py",
]


def _story(**over):
    story = {"files": ["pipeline/foo.py", "REFERENCE.md"], "status": "parked"}
    story.update(over)
    return story


def test_should_list_exactly_the_protected_modules():
    assert PROTECTED_MODULES == frozenset(PROTECTED)


@pytest.mark.parametrize("path", ["setup.py", "Makefile", "CLAUDE.md"])
def test_should_refuse_root_level_path_when_root_file_declared(path):
    allowed, refused = check_scope_amendment(_story(), [path])
    assert allowed == []
    assert refused == {path: "root_level_path"}


@pytest.mark.parametrize("path", PROTECTED)
def test_should_refuse_protected_module_when_sibling_declared(path):
    allowed, refused = check_scope_amendment(_story(), [path])
    assert allowed == []
    assert refused == {path: "protected_module"}


def test_should_report_protected_module_even_at_amendment_limit():
    story = _story(scope_amendments=[{"path": f"pipeline/{c}.py"} for c in "abc"])
    _, refused = check_scope_amendment(story, ["pipeline/scope_gate.py"])
    assert refused == {"pipeline/scope_gate.py": "protected_module"}


@pytest.mark.parametrize("path", ["pipeline/a\x00.py", "pipeline/a\nb.py", "pipeline/a\x7f.py"])
def test_should_refuse_control_characters(path):
    allowed, refused = check_scope_amendment(_story(), [path])
    assert allowed == []
    assert refused == {path: "malformed_path"}


def test_should_still_allow_unprotected_sibling():
    allowed, refused = check_scope_amendment(_story(), ["pipeline/helper_a.py"])
    assert allowed == ["pipeline/helper_a.py"]
    assert refused == {}


def test_should_amend_nothing_when_protected_path_among_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(tsa, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(tsa, "_park", lambda *a, **k: "park_for_human")
    story = _story(scope_conflict={"paths": ["pipeline/helper_a.py", "pipeline/scope_gate.py"]})
    result = _execute_amend_scope(
        "plan", "S", story, {"action": "amend_scope"}, {"stories": {"S": story}}, tmp_path / "m.json"
    )
    assert result == "park_for_human"
    assert story["files"] == ["pipeline/foo.py", "REFERENCE.md"]
    assert "_amended_paths" not in story


def test_should_set_amended_paths_and_not_record_execution(monkeypatch, tmp_path):
    records = []
    monkeypatch.setattr(tsa, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(tsa, "_worktree_head", lambda story: None)
    monkeypatch.setattr(tsa, "_record_execution", lambda *a, **k: records.append(a), raising=False)
    story = _story(scope_conflict={"paths": ["pipeline/helper_a.py"]})
    result = _execute_amend_scope(
        "plan", "S", story, {"action": "amend_scope"}, {"stories": {"S": story}}, tmp_path / "m.json"
    )
    assert result == "amend_scope"
    assert story["_amended_paths"] == ["pipeline/helper_a.py"]
    assert records == []
