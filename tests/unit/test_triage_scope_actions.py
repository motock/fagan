"""Tests for the amend_scope guardrail and executor (HR-03)."""

from __future__ import annotations

import json

import pytest

from pipeline import triage
from pipeline import triage_scope_actions as tsa
from pipeline.triage_scope_actions import _execute_amend_scope, check_scope_amendment

HEAD = "a" * 40


def _story(**over):
    story = {
        "status": "parked",
        "risk": "low",
        "files": ["pipeline/foo.py", "docs/guide.md"],
        "worktree": "/tmp/does-not-matter",
        "scope_conflict": {"paths": ["pipeline/bar.py"], "reasons": {}},
        "review_verdict": "x",
        "last_reviewed_sha": "x",
        "last_reviewed_logic_fingerprint": "x",
        "last_review_findings": ["x"],
        "review_feedback": "x",
    }
    story.update(over)
    return story


# ---- check_scope_amendment -------------------------------------------------


def test_should_allow_sibling_file_in_same_package():
    allowed, refused = check_scope_amendment(_story(), ["pipeline/bar.py"])
    assert allowed == ["pipeline/bar.py"]
    assert refused == {}


def test_should_refuse_everything_for_high_risk_story():
    allowed, refused = check_scope_amendment(_story(risk="high"), ["pipeline/bar.py"])
    assert allowed == []
    assert list(refused) == ["pipeline/bar.py"]


@pytest.mark.parametrize(
    "path",
    ["scripts/x.sh", ".github/w.yml", ".claude/r.md", "docs/adr/1.md", "pyproject.toml",
     ".env", "pipeline/.hidden", "pipeline/a.env", "pipeline/a.plist",
     "requirements-dev.txt", "Dockerfile.dev"],
)
def test_should_refuse_sensitive_paths(path):
    story = _story(files=["pipeline/foo.py", "scripts/a.py", ".github/a.yml", ".claude/a.md",
                          "docs/adr/0.md", "a.py"])
    allowed, refused = check_scope_amendment(story, [path])
    assert allowed == []
    assert path in refused


def test_should_refuse_new_top_level_directory():
    _, refused = check_scope_amendment(_story(), ["newdir/x.py"])
    assert "newdir/x.py" in refused


def test_should_refuse_other_package_directory():
    _, refused = check_scope_amendment(_story(), ["pipeline/sub/x.py"])
    assert "pipeline/sub/x.py" in refused


@pytest.mark.parametrize("path", ["../x.py", "pipeline/../x.py", "/etc/x.py", "pipeline\\x.py"])
def test_should_refuse_traversal_absolute_and_backslash(path):
    allowed, refused = check_scope_amendment(_story(), [path])
    assert allowed == []
    assert path in refused


def test_should_refuse_already_declared_path():
    _, refused = check_scope_amendment(_story(), ["pipeline/foo.py"])
    assert "pipeline/foo.py" in refused


def test_should_refuse_fourth_cumulative_amendment():
    story = _story(scope_amendments=[{"path": "pipeline/a.py"}, {"path": "pipeline/b.py"}])
    allowed, refused = check_scope_amendment(story, ["pipeline/c.py", "pipeline/d.py"])
    assert allowed == ["pipeline/c.py"]
    assert refused == {"pipeline/d.py": "amendment_limit"}


def test_should_allow_third_cumulative_amendment():
    story = _story(scope_amendments=[{"path": "pipeline/a.py"}, {"path": "pipeline/b.py"}])
    allowed, _ = check_scope_amendment(story, ["pipeline/c.py"])
    assert allowed == ["pipeline/c.py"]


def test_should_refuse_when_no_declared_files():
    _, refused = check_scope_amendment(_story(files=[]), ["pipeline/bar.py"])
    assert "pipeline/bar.py" in refused


# ---- _execute_amend_scope --------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    notes, records = [], []
    monkeypatch.setattr(
        tsa, "_notify_user", lambda plan, msg, **kw: notes.append((msg, kw)), raising=False
    )
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        tsa, "_record_execution", lambda *a, **k: records.append((a, k)), raising=False
    )
    monkeypatch.setattr(tsa, "_worktree_head", lambda story: HEAD)
    path = tmp_path / "m.json"
    return notes, records, path


def _run(story, path, rationale="because"):
    manifest = {"stories": {"S": story}}
    return _execute_amend_scope(
        "plan", "S", story, {"action": "amend_scope", "rationale": rationale}, manifest, path
    ), manifest


def test_should_park_when_no_scope_conflict_recorded(env):
    _, _, path = env
    story = _story()
    story.pop("scope_conflict")
    result, _ = _run(story, path)
    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == "amend_scope ruled but no scope conflict is recorded"


def test_should_amend_nothing_when_any_path_refused(env):
    _, _, path = env
    story = _story(scope_conflict={"paths": ["pipeline/bar.py", "scripts/x.sh"]})
    result, _ = _run(story, path)
    assert result == "park_for_human"
    assert story["files"] == ["pipeline/foo.py", "docs/guide.md"]
    assert "scope_amendments" not in story
    assert story["status"] == "parked"
    assert "sensitive_path" in story["parked_reason"]
    assert "scripts/x.sh" not in story["parked_reason"]
    assert not path.exists()


def test_should_append_files_and_record_amendments(env):
    _, _, path = env
    story = _story(scope_conflict={"paths": ["pipeline/bar.py"], "reasons": {"pipeline/bar.py": "r"}})
    result, _ = _run(story, path, rationale="y" * 400)
    assert result == "amend_scope"
    assert story["files"][-1] == "pipeline/bar.py"
    (entry,) = story["scope_amendments"]
    assert entry["path"] == "pipeline/bar.py"
    assert entry["reason"] == "r"
    assert entry["ruling_rationale"] == "y" * 300
    assert entry["at"]


def test_should_clear_review_state_and_conflict(env):
    _, _, path = env
    story = _story()
    _run(story, path)
    for key in ("review_verdict", "last_reviewed_sha", "last_reviewed_logic_fingerprint",
                "last_review_findings", "review_feedback", "scope_conflict"):
        assert key not in story


def test_should_set_tests_passed_on_green_matching_check(env):
    _, _, path = env
    story = _story(last_test_check={"returncode": 0, "sha": HEAD})
    _run(story, path)
    assert story["status"] == "tests_passed"


@pytest.mark.parametrize(
    "check",
    [{"returncode": 1, "sha": HEAD}, {"returncode": 0, "sha": "b" * 40}, None],
)
def test_should_set_todo_otherwise(env, check):
    _, _, path = env
    story = _story()
    if check:
        story["last_test_check"] = check
    _run(story, path)
    assert story["status"] == "todo"


def test_should_write_manifest_notify_and_record_paths_only(env):
    notes, records, path = env
    story = _story()
    _run(story, path)
    assert json.loads(path.read_text())["stories"]["S"]["files"][-1] == "pipeline/bar.py"
    (msg, kw), = notes
    assert kw["event"] == "scope_amended"
    assert "pipeline/bar.py" in msg
    assert story["_amended_paths"] == ["pipeline/bar.py"]
