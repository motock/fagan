"""TDD tests for the story-level ``test_scope`` field.

A story may request ``test_scope`` ``full`` or ``scoped``. ``ingest_plan`` must:

1. VALIDATE ``story["test_scope"]`` up front via ``_test_scope_error`` (wired
   through ``_files_field_error`` in the story loop). Absent or ``None`` is
   valid; any other value must be one of ``"full"`` or ``"scoped"``. The error
   names the story summary and the field.
2. PERSIST ``test_scope`` into the manifest story, and omit the key entirely
   when the plan story has none.
3. RE-INGEST: ``test_scope`` is an authored field allowed to be absent from a
   re-ingested story. Present -> copied; absent -> the key is removed. It is
   refreshed even when the story is no longer ``todo``, and runtime state
   (status, pr_url) is kept.

These tests drive the REAL ``ingest_plan`` with a tmp ``PLAN_DIR``, following
the fixture pattern of ``tests/unit/test_ingest_story_files_field.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401
from pipeline import server as p

# A non-Claude dispatch provider is gated on a real `Preflight:` line
# (.claude/rules/local-dispatch-preflight.md); every fixture story carries one.
_PREFLIGHT = "Preflight: test fixture — not a real plan"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _story(**extra):
    story = {
        "summary": "S1",
        "key": "S1",
        "agent_instructions": _PREFLIGHT + "\nImplement it.",
    }
    story.update(extra)
    return story


def _plan(tmp_path, story):
    return {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [story]}],
    }


def _write_plan(plan_dir, plan_name, plan):
    (plan_dir / f"{plan_name}.json").write_text(json.dumps(plan))


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _write_manifest(plan_dir, plan_name, manifest):
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("test_scope", ["full", "scoped"])
def test_should_copy_story_test_scope_to_manifest(tmp_path, plan_dir, test_scope):
    plan = _plan(tmp_path, _story(test_scope=test_scope))
    _write_plan(plan_dir, "copy", plan)

    result = p.ingest_plan("copy")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "copy")["stories"]["S1"]["test_scope"] == test_scope


def test_should_omit_story_test_scope_key_when_absent(tmp_path, plan_dir):
    _write_plan(plan_dir, "absent", _plan(tmp_path, _story()))

    result = p.ingest_plan("absent")

    assert result["ok"] is True
    assert "test_scope" not in _read_manifest(plan_dir, "absent")["stories"]["S1"]


@pytest.mark.parametrize("test_scope", ["partial", "", 0, {"a": 1}])
def test_should_reject_invalid_story_test_scope_naming_the_story(
    tmp_path, plan_dir, test_scope
):
    _write_plan(plan_dir, "bad", _plan(tmp_path, _story(test_scope=test_scope)))

    result = p.ingest_plan("bad")

    assert result["ok"] is False
    assert "story 'S1' field 'test_scope' must be one of" in result["error"]


def test_should_remove_story_test_scope_on_reingest_when_field_dropped(tmp_path, plan_dir):
    plan = _plan(tmp_path, _story(test_scope="scoped"))
    _write_plan(plan_dir, "drop", plan)
    assert p.ingest_plan("drop")["ok"] is True

    plan["epics"][0]["stories"][0].pop("test_scope")
    _write_plan(plan_dir, "drop", plan)
    result = p.ingest_plan("drop")

    assert result["ok"] is True
    assert "test_scope" not in _read_manifest(plan_dir, "drop")["stories"]["S1"]


def test_should_refresh_story_test_scope_on_reingest_when_story_in_progress(tmp_path, plan_dir):
    plan = _plan(tmp_path, _story(test_scope="full"))
    _write_plan(plan_dir, "refresh", plan)
    assert p.ingest_plan("refresh")["ok"] is True
    manifest = _read_manifest(plan_dir, "refresh")
    manifest["stories"]["S1"]["status"] = "in_progress"
    _write_manifest(plan_dir, "refresh", manifest)

    plan["epics"][0]["stories"][0]["test_scope"] = "scoped"
    _write_plan(plan_dir, "refresh", plan)
    result = p.ingest_plan("refresh")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "refresh")["stories"]["S1"]["test_scope"] == "scoped"


def test_should_keep_runtime_state_on_reingest(tmp_path, plan_dir):
    plan = _plan(tmp_path, _story(test_scope="full"))
    _write_plan(plan_dir, "state", plan)
    assert p.ingest_plan("state")["ok"] is True
    manifest = _read_manifest(plan_dir, "state")
    manifest["stories"]["S1"]["status"] = "in_progress"
    manifest["stories"]["S1"]["pr_url"] = "https://example.invalid/pr/1"
    _write_manifest(plan_dir, "state", manifest)

    plan["epics"][0]["stories"][0]["test_scope"] = "scoped"
    _write_plan(plan_dir, "state", plan)
    assert p.ingest_plan("state")["ok"] is True

    story = _read_manifest(plan_dir, "state")["stories"]["S1"]
    assert story["test_scope"] == "scoped"
    assert story["status"] == "in_progress"
    assert story["pr_url"] == "https://example.invalid/pr/1"


REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"
STORY_SCHEMA_RULE = REPO_ROOT / ".claude" / "rules" / "pipeline-story-schema.md"



def test_should_document_story_test_scope_in_reference():
    text = REFERENCE.read_text()
    assert (
        "A story-level `test_scope` (`full` or `scoped`) is a request that ranks below the"
        in text
    )


def test_should_document_story_test_scope_in_schema_rule():
    text = STORY_SCHEMA_RULE.read_text()
    assert "- `test_scope` (optional, story-level): `full` or `scoped`" in text


def test_should_list_story_test_scope_as_authored_field():
    assert "test_scope" in p._INGEST_AUTHORED_STORY_FIELDS

