"""Tests for the ``patch_plan`` MCP tool (MCPHYG-5).

``patch_plan`` is the plan-level sibling of ``patch_story``: it edits the plan
manifest's TOP-LEVEL fields under the same ``_plan_lock`` discipline, but
allowlisted to exactly ``role_config`` for now. These tests pin the allowlist,
the fail-closed error shape, the locked-skip shape, the missing-plan error, and
the empty-fields boundary.
"""
import fcntl
import json
import os

import pytest

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import _read_manifest


def _write_plan(plan_dir, plan_name, **top):
    """Write a manifest with the given top-level keys (plus empty stories)."""
    manifest = {"epics": {}, "stories": {}, **top}
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    return manifest


def test_patch_plan_updates_role_config_and_persists(plan_dir):
    _write_plan(plan_dir, "pp1", role_config={"reviewer": {"model": "sonnet"}})
    result = p.patch_plan("pp1", {"role_config": {"reviewer": {"model": "opus"}}})
    assert result["ok"] is True
    assert result["plan_name"] == "pp1"
    # Re-read from disk: the write must be persisted, not merely returned.
    manifest = _read_manifest(plan_dir, "pp1")
    assert manifest["role_config"] == {"reviewer": {"model": "opus"}}


def test_patch_plan_preserves_other_top_level_fields(plan_dir):
    _write_plan(plan_dir, "pp1b", role_config={}, repo_root="/tmp/repo")
    result = p.patch_plan("pp1b", {"role_config": {"planner": {"model": "opus"}}})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "pp1b")
    assert manifest["role_config"] == {"planner": {"model": "opus"}}
    assert manifest["repo_root"] == "/tmp/repo"
    assert manifest["stories"] == {}


@pytest.mark.parametrize("field", ["repo_root", "epics", "stories", "made_up_field"])
def test_patch_plan_rejects_field_outside_allowlist(plan_dir, field):
    _write_plan(plan_dir, "pp2", role_config={"a": 1}, repo_root="/tmp/repo")
    before = _read_manifest(plan_dir, "pp2")
    result = p.patch_plan("pp2", {field: "anything"})
    assert result["ok"] is False
    assert field in result["error"]
    assert "role_config" in result["error"]
    # Manifest must be completely unchanged.
    assert _read_manifest(plan_dir, "pp2") == before


def test_patch_plan_rejects_mixed_allowlisted_and_unknown_field(plan_dir):
    _write_plan(plan_dir, "pp2b", role_config={"a": 1})
    before = _read_manifest(plan_dir, "pp2b")
    result = p.patch_plan("pp2b", {"role_config": {"b": 2}, "repo_root": "/x"})
    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert _read_manifest(plan_dir, "pp2b") == before


def test_patch_plan_rejects_nonexistent_plan(plan_dir):
    result = p.patch_plan("no-such-plan", {"role_config": {}})
    assert result["ok"] is False
    assert "no-such-plan" in result["error"]


def test_patch_plan_skips_when_lock_held(plan_dir):
    _write_plan(plan_dir, "pp3", role_config={"a": 1})
    before = _read_manifest(plan_dir, "pp3")
    lock_path = plan_dir / "pp3.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.patch_plan("pp3", {"role_config": {"a": 2}})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert _read_manifest(plan_dir, "pp3") == before


def test_patch_plan_empty_fields_is_noop_success(plan_dir):
    _write_plan(plan_dir, "pp4", role_config={"a": 1})
    before = _read_manifest(plan_dir, "pp4")
    result = p.patch_plan("pp4", {})
    assert result["ok"] is True
    assert result["plan_name"] == "pp4"
    assert _read_manifest(plan_dir, "pp4") == before


def test_patch_plan_rejects_traversal_plan_name(plan_dir):
    with pytest.raises(ValueError):
        p.patch_plan("../escape", {"role_config": {}})
