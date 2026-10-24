"""CL-22: ``.fagan.json`` ``context`` defaults merge beneath plan sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (circular import order).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.repo_config import RepoConfigError, load_repo_config


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


def _repo(tmp_path: Path, fagan: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".fagan.json").write_text(json.dumps({"test_cmd": ["true"], **fagan}))
    return repo


def _write_plan(plan_dir: Path, repo_root: Path, context=None) -> None:
    story = {"summary": "s1", "agent_instructions": "Do it.", "key": "RC-1"}
    plan = {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    if context is not None:
        plan["context"] = context
    (plan_dir / "rcplan.json").write_text(json.dumps(plan))


def _ingest() -> dict:
    return ingest_mod._ingest_plan_impl("rcplan", None, False)


def _sources(plan_dir: Path) -> dict:
    manifest = json.loads((plan_dir / "rcplan.manifest.json").read_text())
    return {s["name"]: s for s in manifest["context"]["sources"]}


def test_repo_only_sources_are_ingested_tagged_repo(plan_dir, tmp_path):
    repo = _repo(tmp_path, {"context": {"sources": [_source()]}})
    _write_plan(plan_dir, repo)

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert sources["org-ux"]["origin"] == "repo"
    assert sources["org-ux"]["max_chars"] == 20000


def test_duplicate_name_resolves_to_plan_source(plan_dir, tmp_path):
    repo = _repo(tmp_path, {"context": {"sources": [_source(uri="/mnt/repo")]}})
    _write_plan(plan_dir, repo, {"sources": [_source(uri="/mnt/plan")]})

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert list(sources) == ["org-ux"]
    assert sources["org-ux"]["uri"] == "/mnt/plan"
    assert sources["org-ux"]["origin"] == "plan"


def test_distinct_names_are_both_kept_with_their_origins(plan_dir, tmp_path):
    repo = _repo(tmp_path, {"context": {"sources": [_source(name="from-repo")]}})
    _write_plan(plan_dir, repo, {"sources": [_source(name="from-plan")]})

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert sources["from-repo"]["origin"] == "repo"
    assert sources["from-plan"]["origin"] == "plan"


def test_invalid_repo_context_rejects_ingest_with_fagan_json_error(plan_dir, tmp_path):
    repo = _repo(tmp_path, {"context": {"sources": [_source(provider="git")]}})
    _write_plan(plan_dir, repo)

    result = _ingest()

    assert result["ok"] is False
    assert "has an invalid .fagan.json" in result["error"]
    assert "provider must be one of" in result["error"]
    assert not (plan_dir / "rcplan.manifest.json").exists()


def test_unknown_top_level_key_still_rejected(tmp_path):
    repo = _repo(tmp_path, {"bogus": 1})

    with pytest.raises(RepoConfigError, match="unknown key in .fagan.json: 'bogus'"):
        load_repo_config(repo)


def test_unknown_key_inside_repo_context_rejected(tmp_path):
    repo = _repo(tmp_path, {"context": {"sources": [_source()], "max_total_chars": 5}})

    with pytest.raises(RepoConfigError, match="unknown key in .fagan.json context"):
        load_repo_config(repo)


def test_non_object_repo_context_rejected(tmp_path):
    repo = _repo(tmp_path, {"context": ["x"]})

    with pytest.raises(RepoConfigError, match="context must be an object"):
        load_repo_config(repo)
