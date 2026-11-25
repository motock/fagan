"""Approve-merge waits for a briefly held plan lock; busy answers 409; plan
detail reports the archived flag.

Covers issue mba-01:
  * pipeline/merge.py `_approve_merge_impl` waits PIPELINE_COMMAND_LOCK_WAIT_SECONDS
    for the plan lock, like every other operator command.
  * app/dashboard_routes_story.py `approve_merge_route` answers 409 (not 404)
    when the service reports a retriable busy refusal.
  * app/dashboard_routes.py `get_plan` reports `archived` for an archived plan.
"""
import fcntl
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import server as p

BUSY_RESULT = {
    "ok": False,
    "error": "plan busy (scheduler tick in progress); retry",
    "retriable": True,
}


def _write_manifest(plan_dir, name, stories):
    import json

    manifest = {"epics": {}, "stories": stories}
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _parked_approved_story(plan_dir, name):
    _write_manifest(plan_dir, name, {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "review_verdict_sha": "stub-head", "risk": "medium", "worktree": "/x"},
    })


def _stub_merge_seams(monkeypatch):
    # Stop at the first merge seam: the lock/wait behaviour is what is under test.
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)


class _FakeService:
    def __init__(self, result):
        self.result = result

    def approve_merge(self, plan_name, story_key):
        return self.result


# ---------- requirement 1: approve_merge waits for a briefly held lock ----------


def test_approve_merge_waits_for_a_briefly_held_plan_lock(plan_dir, monkeypatch):
    """A lock released inside the wait budget must not produce the busy answer."""
    _parked_approved_story(plan_dir, "wait-ok")
    _stub_merge_seams(monkeypatch)
    monkeypatch.setenv("PIPELINE_COMMAND_LOCK_WAIT_SECONDS", "2")

    lock_path = plan_dir / "wait-ok.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_soon():
        time.sleep(0.1)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    releaser = threading.Thread(target=release_soon)
    releaser.start()
    try:
        result = p.approve_merge("wait-ok", "P1")
    finally:
        releaser.join()

    assert result != BUSY_RESULT
    assert result.get("retriable") is not True


def test_approve_merge_still_returns_busy_after_the_wait(plan_dir, monkeypatch):
    """A lock held for the whole wait budget yields the same retriable busy dict."""
    _parked_approved_story(plan_dir, "wait-busy")
    _stub_merge_seams(monkeypatch)
    monkeypatch.setenv("PIPELINE_COMMAND_LOCK_WAIT_SECONDS", "0.2")

    lock_path = plan_dir / "wait-busy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.approve_merge("wait-busy", "P1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result == BUSY_RESULT


# ---------- requirement 2: route maps retriable busy to 409 ----------


def test_approve_merge_route_answers_409_when_the_plan_is_busy(client, monkeypatch):
    monkeypatch.setattr(d, "_service", _FakeService(dict(BUSY_RESULT)))

    res = client.post("/api/plans/busy-plan/stories/P1/approve_merge")

    assert res.status_code == 409
    assert res.json()["detail"] == {
        "reason": "busy",
        "message": "The plan is busy; try again in a moment.",
    }
    assert BUSY_RESULT["error"] not in res.text


def test_approve_merge_route_answers_409_not_reviewed_for_a_non_retriable_refusal(client, monkeypatch):
    monkeypatch.setattr(
        d, "_service", _FakeService({"ok": False, "error": "Story was never reviewer-approved"})
    )

    res = client.post("/api/plans/some-plan/stories/P1/approve_merge")

    assert res.status_code == 409
    assert res.json()["detail"] == {
        "reason": "not_reviewed",
        "message": "The story was never approved by review.",
    }


# ---------- requirement 3: plan detail reports archived ----------


def test_plan_detail_reports_an_archived_plan(client, plan_dir):
    _write_manifest(plan_dir, "x", {"S1": {"summary": "x", "status": "todo"}})
    assert client.post("/api/plans/x/archive").status_code == 200

    res = client.get("/api/plans/x")

    assert res.status_code == 200
    assert res.json()["archived"] is True


def test_plan_detail_reports_a_live_plan_as_not_archived(client, plan_dir):
    _write_manifest(plan_dir, "y", {"S1": {"summary": "x", "status": "todo"}})

    res = client.get("/api/plans/y")

    assert res.status_code == 200
    assert res.json()["archived"] is False
