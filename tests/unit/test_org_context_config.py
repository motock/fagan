"""CL-24: ``PIPELINE_CONTEXT_CONFIG`` org sources merge beneath repo and plan sources."""

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
def notifications(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.delenv("PIPELINE_CONTEXT_CONFIG", raising=False)
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
    events: list[dict] = []

    def _record(plan_name, message, **kwargs):
        events.append({"message": message, **kwargs})

    monkeypatch.setattr(ingest_mod, "_notify_user", _record)
    return events


@pytest.fixture
def plan_dir(notifications):
    return p.PLAN_DIR


def _source(**overrides) -> dict:
    source = {
        "name": "org-ux",
        "provider": "dir",
        "uri": "/mnt/org/ux",
        "phases": ["dispatch"],
    }
    source.update(overrides)
    return source


def _repo(tmp_path: Path, fagan: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".fagan.json").write_text(json.dumps({"test_cmd": ["true"], **(fagan or {})}))
    return repo


def _org_file(tmp_path: Path, monkeypatch, content: str) -> Path:
    path = tmp_path / "org-context.json"
    path.write_text(content)
    monkeypatch.setenv("PIPELINE_CONTEXT_CONFIG", str(path))
    return path


def _write_plan(plan_dir: Path, repo_root: Path, context=None) -> None:
    story = {"summary": "s1", "agent_instructions": "Do it.", "key": "OC-1"}
    plan = {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    if context is not None:
        plan["context"] = context
    (plan_dir / "ocplan.json").write_text(json.dumps(plan))


def _ingest() -> dict:
    return ingest_mod._ingest_plan_impl("ocplan", None, False)


def _manifest(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "ocplan.manifest.json").read_text())


def _sources(plan_dir: Path) -> dict:
    return {s["name"]: s for s in _manifest(plan_dir)["context"]["sources"]}


def test_org_sources_are_ingested_tagged_org(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source()]}))
    _write_plan(plan_dir, _repo(tmp_path))

    assert _ingest()["ok"] is True

    assert _sources(plan_dir)["org-ux"]["origin"] == "org"


def test_org_sources_merge_with_plan_sources(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(name="from-org")]}))
    _write_plan(plan_dir, _repo(tmp_path), {"sources": [_source(name="from-plan")]})

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert sources["from-org"]["origin"] == "org"
    assert sources["from-plan"]["origin"] == "plan"


def test_duplicate_name_loses_to_plan(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(uri="/mnt/org")]}))
    _write_plan(plan_dir, _repo(tmp_path), {"sources": [_source(uri="/mnt/plan")]})

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert list(sources) == ["org-ux"]
    assert sources["org-ux"]["uri"] == "/mnt/plan"
    assert sources["org-ux"]["origin"] == "plan"


def test_duplicate_name_loses_to_repo(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(uri="/mnt/org")]}))
    repo = _repo(tmp_path, {"context": {"sources": [_source(uri="/mnt/repo")]}})
    _write_plan(plan_dir, repo)

    assert _ingest()["ok"] is True

    sources = _sources(plan_dir)
    assert sources["org-ux"]["uri"] == "/mnt/repo"
    assert sources["org-ux"]["origin"] == "repo"


def test_org_sources_sit_beneath_repo_sources(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(name="from-org")]}))
    repo = _repo(tmp_path, {"context": {"sources": [_source(name="from-repo")]}})
    _write_plan(plan_dir, repo)

    assert _ingest()["ok"] is True

    assert list(_sources(plan_dir)) == ["from-org", "from-repo"]


def test_context_resolved_event_names_file_and_count(plan_dir, notifications, tmp_path, monkeypatch):
    path = _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(), _source(name="b")]}))
    _write_plan(plan_dir, _repo(tmp_path))

    assert _ingest()["ok"] is True

    resolved = [e for e in notifications if e.get("event") == "context_resolved"]
    assert len(resolved) == 1
    assert str(path) in resolved[0]["message"]
    assert "2" in resolved[0]["message"]


def test_missing_file_fails_closed(plan_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CONTEXT_CONFIG", str(tmp_path / "absent.json"))
    _write_plan(plan_dir, _repo(tmp_path))

    result = _ingest()

    assert result["ok"] is False
    assert "PIPELINE_CONTEXT_CONFIG" in result["error"]
    assert not (plan_dir / "ocplan.manifest.json").exists()


def test_malformed_json_fails_closed(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, "{not json")
    _write_plan(plan_dir, _repo(tmp_path))

    result = _ingest()

    assert result["ok"] is False
    assert "PIPELINE_CONTEXT_CONFIG" in result["error"]
    assert not (plan_dir / "ocplan.manifest.json").exists()


def test_invalid_org_source_fails_closed(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source(provider="git")]}))
    _write_plan(plan_dir, _repo(tmp_path))

    result = _ingest()

    assert result["ok"] is False
    assert "provider must be one of" in result["error"]


def test_unknown_key_in_org_file_fails_closed(plan_dir, tmp_path, monkeypatch):
    _org_file(tmp_path, monkeypatch, json.dumps({"sources": [_source()], "max_total_chars": 5}))
    _write_plan(plan_dir, _repo(tmp_path))

    result = _ingest()

    assert result["ok"] is False
    assert "PIPELINE_CONTEXT_CONFIG" in result["error"]


def test_relative_path_fails_closed(plan_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CONTEXT_CONFIG", "org-context.json")
    _write_plan(plan_dir, _repo(tmp_path))

    result = _ingest()

    assert result["ok"] is False
    assert "absolute" in result["error"]


def test_unset_leaves_behavior_unchanged(plan_dir, notifications, tmp_path):
    _write_plan(plan_dir, _repo(tmp_path))

    assert _ingest()["ok"] is True

    assert "context" not in _manifest(plan_dir)
    assert not [e for e in notifications if e.get("event") == "context_resolved"]


def test_unset_keeps_plan_sources_tagged_plan(plan_dir, tmp_path):
    _write_plan(plan_dir, _repo(tmp_path), {"sources": [_source()]})

    assert _ingest()["ok"] is True

    assert _sources(plan_dir)["org-ux"]["origin"] == "plan"
