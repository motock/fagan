"""check_story_status returns a structured result for finished stories and
unknown plans instead of raising FileNotFoundError."""

import json

import pytest

from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)

    def _fail(*_args, **_kwargs):
        raise AssertionError("detect_test_command must not be called")

    monkeypatch.setattr(p, "detect_test_command", _fail)
    return d


def _write_manifest(plan_dir, stories):
    path = plan_dir / "plan.manifest.json"
    path.write_text(json.dumps({"epics": {}, "stories": stories}, indent=2))
    return path


def _story(status, **extra):
    return {"status": status, "worktree": "/nonexistent/worktree", **extra}


def test_should_return_status_and_pid_for_done_story(plan_dir):
    _write_manifest(plan_dir, {"S1": _story("done", pid=4242)})

    assert p.check_story_status("plan", "S1") == {"status": "done", "pid": 4242}


def test_should_return_status_and_pid_for_merged_story(plan_dir):
    _write_manifest(plan_dir, {"S1": _story("merged", pid=4242)})

    assert p.check_story_status("plan", "S1") == {"status": "merged", "pid": 4242}


def test_should_not_modify_manifest_for_terminal_story(plan_dir):
    path = _write_manifest(plan_dir, {"S1": _story("done", pid=4242)})
    before = path.read_bytes()

    p.check_story_status("plan", "S1")

    assert path.read_bytes() == before


def test_should_report_plan_not_found_without_leaking_path(plan_dir):
    result = p.check_story_status("no-such-plan", "X")

    assert result == {"ok": False, "error": "Plan not found"}
    assert str(plan_dir) not in json.dumps(result)


def test_should_report_story_not_dispatched_for_unknown_key(plan_dir):
    _write_manifest(plan_dir, {"S1": _story("done", pid=4242)})

    assert p.check_story_status("plan", "MISSING") == {
        "ok": False,
        "error": "Story not dispatched",
    }


def test_should_still_return_interrupted_story(plan_dir):
    _write_manifest(plan_dir, {"S1": _story("interrupted", pid=4242)})

    assert p.check_story_status("plan", "S1") == {"status": "interrupted", "pid": 4242}


def test_should_report_not_dispatched_for_done_story_without_pid(plan_dir):
    _write_manifest(plan_dir, {"S1": _story("done")})

    assert p.check_story_status("plan", "S1") == {
        "ok": False,
        "error": "Story not dispatched",
    }
