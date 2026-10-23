"""CL-11: ingest validates the plan-level ``context`` block and carries it
into the manifest, preserving the prior manifest's block when omitted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (circular import order).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wt)

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _Provider())
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return d


def _source(**overrides) -> dict:
    source = {
        "name": "org-ux",
        "provider": "dir",
        "uri": "/mnt/org/ux",
        "phases": ["dispatch"],
    }
    source.update(overrides)
    return source


def _write_plan(plan_dir: Path, repo_root: Path, context=None) -> None:
    story = {"summary": "s1", "agent_instructions": "Do it.", "key": "CTX-1"}
    plan = {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    if context is not None:
        plan["context"] = context
    (plan_dir / "ctxplan.json").write_text(json.dumps(plan))


def _ingest() -> dict:
    return ingest_mod._ingest_plan_impl("ctxplan", None, False)


def _manifest(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "ctxplan.manifest.json").read_text())


def test_valid_context_is_carried_into_manifest_with_defaults(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path, {"sources": [_source()]})

    assert _ingest()["ok"] is True

    context = _manifest(plan_dir)["context"]
    assert context["max_total_chars"] == 60000
    assert context["sources"][0]["name"] == "org-ux"
    assert context["sources"][0]["max_chars"] == 20000


def test_invalid_context_rejects_ingest_with_all_errors_joined(plan_dir, tmp_path):
    bad = [_source(provider="git"), _source(name="Bad Name", uri="rel")]
    _write_plan(plan_dir, tmp_path, {"sources": bad})

    result = _ingest()

    assert result["ok"] is False
    assert "provider must be one of" in result["error"]
    assert "name must match" in result["error"]
    assert not (plan_dir / "ctxplan.manifest.json").exists()


def test_unknown_source_key_rejects_ingest(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path, {"sources": [_source(bogus=1)]})

    result = _ingest()

    assert result["ok"] is False
    assert "unknown key 'bogus'" in result["error"]


def test_unknown_plan_level_key_rejects_ingest(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path, {"sources": [_source()], "bogus": 1})

    result = _ingest()

    assert result["ok"] is False
    assert "unknown key 'bogus'" in result["error"]


def test_non_object_context_rejects_ingest(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path, ["not", "an", "object"])

    result = _ingest()

    assert result["ok"] is False
    assert "context must be an object" in result["error"]


def test_omitted_context_preserves_prior_manifest_context(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path, {"sources": [_source()], "max_total_chars": 5000})
    assert _ingest()["ok"] is True
    prior = _manifest(plan_dir)["context"]

    _write_plan(plan_dir, tmp_path)
    assert _ingest()["ok"] is True

    assert _manifest(plan_dir)["context"] == prior


def test_omitted_context_with_no_prior_adds_no_context_key(plan_dir, tmp_path):
    _write_plan(plan_dir, tmp_path)

    assert _ingest()["ok"] is True

    assert "context" not in _manifest(plan_dir)
