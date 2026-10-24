"""HR-02: a no-commit turn that reports SCOPE-CONFLICT parks without
charging the rework budget; a marker-free no-commit turn still does."""
import json

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline.no_progress import classify_no_progress
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)

MARKER_A = "SCOPE-CONFLICT: pipeline/a.py :: needs a change here"
MARKER_B = "SCOPE-CONFLICT: pipeline/b.py :: and here"


def _log(worktree, *done_lines):
    body = "[step 1] bash: pytest -q\n"
    for i, line in enumerate(done_lines, start=4):
        body += f"[step {i}] DONE: {line}\n"
    (worktree / "agent.log").write_text(body)


def _setup(tmp_path, monkeypatch, done_text):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if done_text is not None:
        _log(worktree, done_text)
    story = {"summary": "s", "status": "tests_passed", "rework_attempts": 0}
    manifest = {"epics": {}, "stories": {"S1": story}}
    path = plan_dir / "plan.manifest.json"
    path.write_text(json.dumps(manifest))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notes.append((a, k)))
    return story, manifest, path, worktree, notes


def _run(story, worktree, manifest, path):
    return classify_no_progress("plan", "S1", story, str(worktree), manifest, path)


def test_marker_parks_story_with_reason(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, MARKER_A)
    result = _run(story, wt, manifest, path)
    assert result == {"status": "parked", "reason": "scope_conflict"}
    assert story["status"] == "parked"
    assert story["parked_reason"] == "scope conflict: pipeline/a.py"


def test_marker_records_scope_conflict(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, MARKER_A)
    _run(story, wt, manifest, path)
    rec = story["scope_conflict"]
    assert rec["paths"] == ["pipeline/a.py"]
    assert rec["reasons"] == {"pipeline/a.py": "needs a change here"}
    assert rec["recorded_at"]


def test_marker_leaves_rework_attempts_unchanged(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, MARKER_A)
    story["rework_attempts"] = 1
    _run(story, wt, manifest, path)
    assert story["rework_attempts"] == 1


def test_marker_writes_manifest(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, MARKER_A)
    _run(story, wt, manifest, path)
    on_disk = json.loads(path.read_text())["stories"]["S1"]
    assert on_disk["status"] == "parked"


def test_marker_notifies_once(tmp_path, monkeypatch):
    story, manifest, path, wt, notes = _setup(tmp_path, monkeypatch, MARKER_A)
    story["correlation_id"] = "cid-1"
    _run(story, wt, manifest, path)
    assert len(notes) == 1
    assert notes[0][1]["event"] == "story_parked"
    assert notes[0][1]["correlation_id"] == "cid-1"


def test_two_markers_in_one_summary_record_both(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, None)
    from pipeline import no_progress
    monkeypatch.setattr(
        no_progress, "_last_done_summary", lambda _p: MARKER_A + "\n" + MARKER_B)
    _run(story, wt, manifest, path)
    assert story["scope_conflict"]["paths"] == ["pipeline/a.py", "pipeline/b.py"]
    assert story["parked_reason"] == "scope conflict: pipeline/a.py, pipeline/b.py"


def test_no_marker_returns_none_and_touches_nothing(tmp_path, monkeypatch):
    story, manifest, path, wt, notes = _setup(tmp_path, monkeypatch, "all finished")
    assert _run(story, wt, manifest, path) is None
    assert story == {"summary": "s", "status": "tests_passed", "rework_attempts": 0}
    assert notes == []


def test_missing_log_returns_none(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, None)
    assert _run(story, wt, manifest, path) is None
    assert "scope_conflict" not in story


def test_unreadable_log_returns_none(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, MARKER_A)
    from pipeline import no_progress

    def boom(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(no_progress, "_last_done_summary", boom)
    assert _run(story, wt, manifest, path) is None
    assert story["status"] == "tests_passed"


# ---------- end to end through the story_status guard ----------

HEAD_SHA = "abc123"


def _e2e(tmp_path, monkeypatch, done_text):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    for mod in (p, ppers, pcon):
        monkeypatch.setattr(mod, "PLAN_DIR", plan_dir)
    worktree = plan_dir / "wt"
    worktree.mkdir()
    _log(worktree, done_text)
    _write_manifest(plan_dir, "plan", {"S1": {
        "summary": "thing", "status": "in_progress", "pid": 4242,
        "worktree": str(worktree), "rework_attempts": 0,
        "last_reviewed_sha": HEAD_SHA, "agent_instructions": "GOAL",
    }})
    monkeypatch.setattr(
        p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_escalate_review_to_claude", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)

    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.returncode = 0

    monkeypatch.setattr(
        p.subprocess, "run",
        lambda *a, **k: Result(HEAD_SHA + "\n" if "rev-parse" in a[0] else ""))
    return plan_dir


def test_guard_parks_marker_run_without_charging_budget(tmp_path, monkeypatch):
    plan_dir = _e2e(tmp_path, monkeypatch, MARKER_A)
    result = p.check_story_status("plan", "S1")
    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert result == {"status": "parked", "reason": "scope_conflict"}
    assert story["rework_attempts"] == 0


def test_guard_still_charges_marker_free_no_commit_run(tmp_path, monkeypatch):
    plan_dir = _e2e(tmp_path, monkeypatch, "gave up")
    result = p.check_story_status("plan", "S1")
    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert result["reason"] == "no_new_commit_since_last_review"
    assert story["rework_attempts"] == 1
