"""TDD tests for the plan-level ``test_scope`` field at ``ingest_plan``.

A plan may name ``test_scope`` as ``"full"`` or ``"scoped"``. ``ingest_plan``
must:

1. VALIDATE a present plan-level ``test_scope`` up front, before any side
   effect, via ``pipeline.ingest_plan_fields._test_scope_error``. ``None`` when
   the key is absent or ``None``; otherwise the value must be one of the
   strings in ``TEST_SCOPE_VALUES``. Unhashable or non-string values are
   rejected without raising.
2. PERSIST the value on the manifest, and omit the key when the plan has none.
3. RE-INGEST: an absent plan-level ``test_scope`` keeps the prior value; a
   present one replaces it.

It also pins that moving ``_TEAM_RE`` / ``_team_error`` out of ``ingest.py``
kept the names importable from ``pipeline.ingest``, and that ``ingest.py``
stays within the 1000-line production limit.

These tests drive the REAL ``_ingest_plan_impl`` through ``p.ingest_plan``
with a tmp ``PLAN_DIR``, following ``tests/unit/test_ingest_story_files_field.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401
from pipeline import ingest as ingest_mod
from pipeline import ingest_plan_fields
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._line_limit_policy import assert_within_line_limit

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"
STORY_SCHEMA_RULE = REPO_ROOT / ".claude" / "rules" / "pipeline-story-schema.md"
INGEST_PY = REPO_ROOT / "pipeline" / "ingest.py"

# A non-Claude dispatch provider is gated on a real `Preflight:` line.
_PREFLIGHT = "Preflight: test fixture — not a real plan"
_SCOPE_ERROR = "plan field 'test_scope' must be one of 'full', 'scoped'"


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _story(**extra):
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": _PREFLIGHT + "\nImplement it.",
    }
    story.update(extra)
    return story


def _plan(tmp_path, story, **extra):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    plan.update(extra)
    return plan


def _write_plan(plan_dir, plan_name, plan):
    (plan_dir / f"{plan_name}.json").write_text(json.dumps(plan))


def _manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _read_manifest(plan_dir, plan_name):
    return json.loads(_manifest_path(plan_dir, plan_name).read_text())


# ---------------------------------------------------------------------------
# 1. Persistence of a valid plan-level test_scope
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scope", ["scoped", "full"])
def test_should_persist_plan_test_scope(plan_dir, monkeypatch, tmp_path, scope):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "persist", _plan(tmp_path, _story(), test_scope=scope))

    result = p.ingest_plan("persist")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "persist")["test_scope"] == scope


def test_should_omit_test_scope_from_manifest_when_plan_has_none(
    plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "absent", _plan(tmp_path, _story()))

    assert p.ingest_plan("absent")["ok"] is True
    assert "test_scope" not in _read_manifest(plan_dir, "absent")


# ---------------------------------------------------------------------------
# 2. Validation: invalid values are rejected before any side effect
# ---------------------------------------------------------------------------
_INVALID_SCOPES = ["partial", "Full", "", 1, ["scoped"], True]


@pytest.mark.parametrize("scope", _INVALID_SCOPES)
def test_should_reject_invalid_plan_test_scope(plan_dir, monkeypatch, tmp_path, scope):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "badscope", _plan(tmp_path, _story(), test_scope=scope))

    result = p.ingest_plan("badscope")

    assert result["ok"] is False
    assert _SCOPE_ERROR in result["error"]
    assert not _manifest_path(plan_dir, "badscope").exists()


def test_should_accept_explicit_null_test_scope(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "nullscope", _plan(tmp_path, _story(), test_scope=None))

    assert p.ingest_plan("nullscope")["ok"] is True
    assert "test_scope" not in _read_manifest(plan_dir, "nullscope")


def test_helper_returns_none_for_valid_values():
    assert ingest_plan_fields._test_scope_error({"test_scope": "scoped"}) is None
    assert ingest_plan_fields._test_scope_error({"test_scope": "full"}) is None
    assert ingest_plan_fields._test_scope_error({}) is None


def test_helper_names_label_and_value_for_invalid_value():
    err = ingest_plan_fields._test_scope_error({"test_scope": "partial"}, label="plan")
    assert err == (
        "plan field 'test_scope' must be one of 'full', 'scoped', got 'partial'"
    )


# ---------------------------------------------------------------------------
# 3. Re-ingest semantics
# ---------------------------------------------------------------------------
def test_should_keep_prior_plan_test_scope_on_reingest_without_field(
    plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "keep", _plan(tmp_path, _story(), test_scope="scoped"))
    assert p.ingest_plan("keep")["ok"] is True

    _write_plan(plan_dir, "keep", _plan(tmp_path, _story()))
    assert p.ingest_plan("keep")["ok"] is True

    assert _read_manifest(plan_dir, "keep")["test_scope"] == "scoped"


def test_should_replace_plan_test_scope_on_reingest_with_new_value(
    plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "replace", _plan(tmp_path, _story(), test_scope="scoped"))
    assert p.ingest_plan("replace")["ok"] is True

    _write_plan(plan_dir, "replace", _plan(tmp_path, _story(), test_scope="full"))
    assert p.ingest_plan("replace")["ok"] is True

    assert _read_manifest(plan_dir, "replace")["test_scope"] == "full"


# ---------------------------------------------------------------------------
# 4. Regression: team validation survives the move to ingest_plan_fields
# ---------------------------------------------------------------------------
def test_should_still_reject_bad_team(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "badteam", _plan(tmp_path, _story(), team="Bad Team"))

    result = p.ingest_plan("badteam")

    assert result["ok"] is False
    assert "plan field 'team' must be a lowercase slug" in result["error"]
    assert not _manifest_path(plan_dir, "badteam").exists()


def test_team_error_is_reexported_from_ingest():
    assert ingest_mod._team_error is ingest_plan_fields._team_error
    assert ingest_mod._TEAM_RE is ingest_plan_fields._TEAM_RE


def test_ingest_module_stays_within_line_limit():
    with INGEST_PY.open() as source:
        line_count = sum(1 for _ in source)
    assert_within_line_limit(line_count, 1000, "pipeline/ingest.py")


# ---------------------------------------------------------------------------
# 5. Documentation pins
# ---------------------------------------------------------------------------
def test_should_document_plan_test_scope():
    schema = STORY_SCHEMA_RULE.read_text()
    reference = REFERENCE.read_text()
    assert "- `test_scope` (optional, plan-level)" in schema
    assert "## Test scope (`test_scope`)" in reference
    assert (
        "The plan-level `test_scope` field (a sibling of `epics`, like `role_config`) is"
        in reference
    )
