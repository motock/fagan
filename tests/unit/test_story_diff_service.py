import copy
import json

import pytest

from pipeline import server
from pipeline.service import PipelineService


@pytest.fixture
def service():
    return PipelineService()


def _manifest(worktree=None):
    story = {"summary": "s", "status": "in_progress"}
    if worktree is not None:
        story["worktree"] = str(worktree)
    return {"stories": {"S-1": story}}


def test_should_return_not_found_for_unknown_story(service):
    result = service.get_story_diff("plan", "NOPE", _manifest())

    assert result == {"ok": False, "status": 404, "error": "unknown plan/story"}


def test_should_return_not_found_for_unknown_plan_manifest(service):
    result = service.get_story_diff("missing", "S-1", {})

    assert result == {"ok": False, "status": 404, "error": "unknown plan/story"}


def test_should_report_no_worktree_when_story_has_none(service):
    result = service.get_story_diff("plan", "S-1", _manifest())

    assert result == {"available": False, "reason": "no_worktree"}


def test_should_report_no_worktree_for_path_request_without_worktree(service):
    result = service.get_story_diff("plan", "S-1", _manifest(), path="a.py")

    assert result == {"available": False, "reason": "no_worktree"}


def test_should_report_no_worktree_when_outside_worktree_root(service, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(server, "WORKTREE_ROOT", root)

    result = service.get_story_diff("plan", "S-1", _manifest(outside))

    assert result == {"available": False, "reason": "no_worktree"}


def test_should_use_worktree_root_patched_on_pipeline_server(service, tmp_path, monkeypatch):
    root = tmp_path / "root"
    wt = root / "S-1"
    wt.mkdir(parents=True)
    monkeypatch.setattr(server, "WORKTREE_ROOT", root)

    result = service.get_story_diff("plan", "S-1", _manifest(wt))

    assert result["available"] is False
    assert result["reason"] == "not_a_worktree"


def test_should_not_mutate_manifest(service, tmp_path, monkeypatch):
    root = tmp_path / "root"
    wt = root / "S-1"
    wt.mkdir(parents=True)
    monkeypatch.setattr(server, "WORKTREE_ROOT", root)
    manifest = _manifest(wt)
    before = json.dumps(manifest, sort_keys=True)
    snapshot = copy.deepcopy(manifest)

    service.get_story_diff("plan", "S-1", manifest)

    assert json.dumps(manifest, sort_keys=True) == before
    assert manifest == snapshot
