"""``unblock_story`` tells the operator when a review will skip the story.

A story sent to ``tests_passed`` that keeps its ``last_reviewed_sha`` is
declined by ``review_story`` until its branch moves (the unchanged-since-review
guard). The tool result carries a fixed ``review_hint`` code in that case so
the operator is not left waiting. The hint is a result key only: it is never
written to the story or to the human resolution record.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from app.identity import Principal
from pipeline import persistence as ppers
from pipeline import plan_approval
from pipeline import server_tools_lifecycle as lifecycle
from pipeline import unpark as unpark_mod
from pipeline.actor import bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.store import FileStore

PLAN = "forge"
STORY = "S1"
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
SHA = "a" * 40
HINT = "unchanged_since_last_review"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    return d


@pytest.fixture
def resolutions(monkeypatch):
    """Record what the unblock would write to the human resolution record."""
    seen: list[dict] = []
    monkeypatch.setattr(
        unpark_mod,
        "_notify_user",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        unpark_mod,
        "_record_resolution",
        lambda result, **kwargs: seen.append({"result": result, **kwargs}),
    )
    return seen


def _write(**story_fields) -> None:
    story = {
        "summary": "s",
        "status": "parked",
        "agent_instructions": "Do the thing.",
        "files": ["a.py"],
        "rework_attempts": 3,
        "parked_reason": "x",
    }
    story.update(story_fields)
    FileStore().save_manifest(
        PLAN, {"plan": PLAN, "owner": "https://idp#alice", "stories": {STORY: story}}
    )


def _story() -> dict:
    return FileStore().get_manifest(PLAN)["stories"][STORY]


def _call(**kwargs):
    args = {"plan_name": PLAN, "story_key": STORY, "clear_fields": ["rework_attempts"]}
    args.update(kwargs)
    with bind_principal(ALICE):
        return lifecycle.unblock_story(**args)


def test_unblock_to_tests_passed_keeping_last_reviewed_sha_hints_skip(
    plan_dir, resolutions
):
    _write(last_reviewed_sha=SHA)
    result = _call(status="tests_passed")
    assert result["ok"] is True
    assert result["review_hint"] == HINT


def test_clearing_last_reviewed_sha_with_tests_passed_has_no_hint(
    plan_dir, resolutions
):
    _write(last_reviewed_sha=SHA)
    result = _call(
        clear_fields=["rework_attempts", "last_reviewed_sha"], status="tests_passed"
    )
    assert result["ok"] is True
    assert "review_hint" not in result
    assert "last_reviewed_sha" not in _story()


@pytest.mark.parametrize("status", ["interrupted", None])
def test_other_target_status_keeping_last_reviewed_sha_has_no_hint(
    plan_dir, resolutions, status
):
    _write(last_reviewed_sha=SHA)
    result = _call(status=status)
    assert result["ok"] is True
    assert "review_hint" not in result


def test_story_without_last_reviewed_sha_has_no_hint(plan_dir, resolutions):
    _write()
    result = _call(status="tests_passed")
    assert result["ok"] is True
    assert "review_hint" not in result


def test_empty_last_reviewed_sha_has_no_hint(plan_dir, resolutions):
    _write(last_reviewed_sha="")
    result = _call(status="tests_passed")
    assert result["ok"] is True
    assert "review_hint" not in result


def test_hint_is_not_written_to_the_resolution_record_or_the_story(
    plan_dir, resolutions
):
    _write(last_reviewed_sha=SHA)
    result = _call(status="tests_passed")
    assert result["review_hint"] == HINT
    assert len(resolutions) == 1
    assert "review_hint" not in resolutions[0]["result"]
    assert "review_hint" not in json.dumps(resolutions[0])
    assert "review_hint" not in _story()


def test_refused_unblock_carries_no_hint_and_changes_nothing(plan_dir, resolutions):
    _write(last_reviewed_sha=SHA)
    result = _call(clear_fields=["files"], status="tests_passed")
    assert result["ok"] is False
    assert "review_hint" not in result
    story = _story()
    assert story["last_reviewed_sha"] == SHA
    assert story["files"] == ["a.py"]
    assert story["rework_attempts"] == 3
    assert story["status"] == "parked"
    assert resolutions == []
