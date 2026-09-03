import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest

# Helpers from existing tests
from pipeline import server as _server
from pipeline.story_status import check_story_status, start_detached_grade, collect_detached_grade

# Fixtures similar to existing tests
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Create a fake plan directory with a manifest and state root."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    # Write a fake manifest
    manifest_path = plan_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"plan_name": "demo", "stories": []}))
    # Ensure the plan root is the parent of the manifest
    # (no need to set _server.MANIFEST_PATH for this test)
    return plan_dir

@pytest.fixture
def state_root(plan_dir):
    return plan_dir.parent

@pytest.fixture
def worktree(tmp_path):
    return tmp_path / "worktree"

# Helper to run check_story_status with minimal required args

def run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path):
    return check_story_status(
        story,
        plan_name,
        plan_dir,
        worktree,
        manifest_path,
        None,
        None,
        None,
        None,
    )

# Test that default grading channel is outside worktree

def test_default_grading_channel_outside_worktree(plan_dir, state_root, worktree, monkeypatch):
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    story_key = "0042"
    story = {
        "story_key": story_key,
        "status": "in_progress",
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    # The spawn branch should have persisted a grading_result_path
    grading_result_path = Path(story.get("grading_result_path"))
    assert grading_result_path.is_absolute()
    # It must not be inside the worktree
    assert not grading_result_path.resolve().is_relative_to(worktree.resolve())
    # The path should be under the state root
    assert grading_result_path.resolve().is_relative_to(state_root.resolve())
    # The result file should exist in the state root
    assert grading_result_path.exists()
    # The log file should also exist
    log_path = grading_result_path.parent / "grading.log"
    assert log_path.exists()

# Test that setting PIPELINE_STATE_DIR overrides the default

def test_pipeline_state_dir_override(plan_dir, worktree, monkeypatch):
    override_dir = worktree / "override_state"
    override_dir.mkdir()
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(override_dir))
    story_key = "0043"
    story = {
        "story_key": story_key,
        "status": "in_progress",
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    grading_result_path = Path(story.get("grading_result_path"))
    assert grading_result_path.resolve().is_relative_to(override_dir.resolve())
    # It should not be under the worktree
    assert not grading_result_path.resolve().is_relative_to(worktree.resolve())
    # The log file should exist
    log_path = grading_result_path.parent / "grading.log"
    assert log_path.exists()

# Test that a dead grading_pid collects the result correctly

def test_dead_grading_pid_collects_result(plan_dir, state_root, worktree, monkeypatch):
    story_key = "0044"
    story = {
        "story_key": story_key,
        "status": "in_progress",
        "grading_pid": 999999,  # dead pid
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    # Prepare a result file in the state root
    grading_dir = state_root / "grading" / story_key
    grading_dir.mkdir(parents=True, exist_ok=True)
    result_path = grading_dir / "result.json"
    result_path.write_text(json.dumps({"returncode": 0, "stdout": "ok", "stderr": ""}))
    story["grading_result_path"] = str(result_path)
    # Run check_story_status; it should collect and set tests_passed
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    assert story.get("tests_passed") is True
    assert story.get("grading_pid") is None
    assert story.get("grading_started_at") is None
    assert story.get("grading_result_path") is None
    assert result["status"] == "tests_passed"

# Test that a live grading_pid returns grading status without spawning

def test_live_grading_pid_returns_grading(plan_dir, state_root, worktree, monkeypatch):
    story_key = "0045"
    # Use a real pid that exists (current process)
    live_pid = os.getpid()
    story = {
        "story_key": story_key,
        "status": "in_progress",
        "grading_pid": live_pid,
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    assert result["status"] == "grading"
    assert result["pid"] == live_pid
    # No new grading_result_path should be set
    assert story.get("grading_result_path") is None

# Test that a dead grading_pid with no result file triggers fail-closed

def test_dead_grading_pid_no_result_file(plan_dir, state_root, worktree, monkeypatch):
    story_key = "0046"
    story = {
        "story_key": story_key,
        "status": "in_progress",
        "grading_pid": 999999,  # dead pid
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    # Ensure no result file exists
    grading_dir = state_root / "grading" / story_key
    grading_dir.mkdir(parents=True, exist_ok=True)
    result_path = grading_dir / "result.json"
    if result_path.exists():
        result_path.unlink()
    story["grading_result_path"] = str(result_path)
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    # Should fail closed
    assert story.get("tests_passed") is False
    assert result["status"] == "failed"
    assert "detached grade exited without writing a result" in result["stderr"]

# Test that the grading watchdog triggers after timeout

def test_grading_watchdog_triggers(plan_dir, state_root, worktree, monkeypatch):
    story_key = "0047"
    story = {
        "story_key": story_key,
        "status": "in_progress",
        "grading_pid": 999999,
        "grading_started_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
    }
    plan_name = "demo"
    manifest_path = plan_dir / "manifest.json"
    grading_dir = state_root / "grading" / story_key
    grading_dir.mkdir(parents=True, exist_ok=True)
    result_path = grading_dir / "result.json"
    result_path.write_text(json.dumps({"returncode": 0, "stdout": "ok", "stderr": ""}))
    story["grading_result_path"] = str(result_path)
    result = run_check_story_status(story, plan_name, plan_dir, worktree, manifest_path)
    # The watchdog should have fired, marking failure
    assert story.get("tests_passed") is False
    assert result["status"] == "failed"
    assert "grading watchdog fired" in result["stderr"]

