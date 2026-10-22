"""``blocked_by`` is a patchable story field (XP-2).

``blocked_by`` is a list of ``"<plan>:<story_key>"`` strings. These tests pin
the service-level validation, the allowlist boundary, and the dashboard PATCH
route forwarding.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import ci, service
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import _read_manifest


def _write_plan(plan_dir, plan_name, **story_extra):
    story = {"summary": "s", "status": "ready", **story_extra}
    manifest = {"epics": {}, "stories": {"S1": story}, "repo_root": "/tmp"}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def test_patchable_fields_include_blocked_by_in_both_copies():
    assert "blocked_by" in service._PATCHABLE_STORY_FIELDS
    assert "blocked_by" in ci._PATCHABLE_STORY_FIELDS


def test_patchable_field_sets_are_identical():
    assert service._PATCHABLE_STORY_FIELDS == ci._PATCHABLE_STORY_FIELDS


def test_patch_story_persists_blocked_by_verbatim(plan_dir):
    _write_plan(plan_dir, "bp1")
    result = p.patch_story("bp1", "S1", {"blocked_by": ["plan:KEY", "other:2"]})
    assert result["ok"] is True
    stories = _read_manifest(plan_dir, "bp1")["stories"]
    assert stories["S1"]["blocked_by"] == ["plan:KEY", "other:2"]


def test_patch_story_accepts_empty_blocked_by(plan_dir):
    _write_plan(plan_dir, "bp2", blocked_by=["plan:KEY"])
    result = p.patch_story("bp2", "S1", {"blocked_by": []})
    assert result["ok"] is True
    assert _read_manifest(plan_dir, "bp2")["stories"]["S1"]["blocked_by"] == []


@pytest.mark.parametrize("bad", ["plan:KEY", {"a": "b"}, 7, None])
def test_patch_story_rejects_non_list_blocked_by(plan_dir, bad):
    _write_plan(plan_dir, "bp3")
    result = p.patch_story("bp3", "S1", {"blocked_by": bad})
    assert result["ok"] is False
    assert "blocked_by" in result["error"]
    assert "blocked_by" not in _read_manifest(plan_dir, "bp3")["stories"]["S1"]


@pytest.mark.parametrize("bad", [["plan:KEY", 3], [None], [["p:1"]]])
def test_patch_story_rejects_non_string_blocked_by_entry(plan_dir, bad):
    _write_plan(plan_dir, "bp4")
    result = p.patch_story("bp4", "S1", {"blocked_by": bad})
    assert result["ok"] is False
    assert "blocked_by" in result["error"]
    assert "blocked_by" not in _read_manifest(plan_dir, "bp4")["stories"]["S1"]


def test_patch_story_without_blocked_by_leaves_it_unchanged(plan_dir):
    _write_plan(plan_dir, "bp5", blocked_by=["plan:KEY"])
    result = p.patch_story("bp5", "S1", {"summary": "new"})
    assert result["ok"] is True
    assert _read_manifest(plan_dir, "bp5")["stories"]["S1"]["blocked_by"] == ["plan:KEY"]


def test_patch_story_still_refuses_field_outside_the_set(plan_dir):
    _write_plan(plan_dir, "bp6")
    result = p.patch_story("bp6", "S1", {"files": ["a.py"]})
    assert result["ok"] is False
    assert "files" in result["error"]


def test_patch_route_forwards_and_persists_blocked_by(plan_dir):
    _write_plan(plan_dir, "bp7")
    res = TestClient(d.app).post(
        "/api/plans/bp7/stories/S1/patch", json={"blocked_by": ["p:1"]}
    )
    assert res.status_code == 200
    assert _read_manifest(plan_dir, "bp7")["stories"]["S1"]["blocked_by"] == ["p:1"]


def test_patch_route_rejects_non_list_blocked_by(plan_dir):
    _write_plan(plan_dir, "bp8")
    res = TestClient(d.app).post(
        "/api/plans/bp8/stories/S1/patch", json={"blocked_by": "p:1"}
    )
    assert res.status_code == 422
