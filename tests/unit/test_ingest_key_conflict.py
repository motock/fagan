"""TOPO-3: ingest rejects an explicit story key another plan still holds.

A story's branch (``agent/<key>``) and worktree path are derived from its key
alone, so two plans sharing a key would share both. ``_ingest_plan_impl``
consults ``pipeline.cross_plan.key_conflicts`` before the transaction so a
rejected ingest writes no manifest and makes no ticket-provider call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MD = REPO_ROOT / ".claude" / "rules" / "pipeline-story-schema.md"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


class _RecordingProvider:
    enabled = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_epic(self, summary):
        self.calls.append("create_epic")

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append("create_story")


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return prov


def _write_plan(plan_dir: Path, name: str, epics: list[dict], repo_root: Path) -> None:
    plan = {"name": name, "repo_root": str(repo_root), "epics": epics}
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _plan_with_story(plan_dir, name, repo_root, key, epic="E1"):
    story = {"summary": f"story {key}", "agent_instructions": "Do it.", "key": key}
    _write_plan(plan_dir, name, [{"summary": epic, "stories": [story]}], repo_root)


def _write_other_manifest(plan_dir: Path, plan: str, key: str, status: str) -> None:
    manifest = {"epics": {}, "stories": {key: {"status": status}}, "repo_root": "/x"}
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(manifest))


def _ingest(plan_name: str, only_epics=None) -> dict:
    return ingest_mod._ingest_plan_impl(plan_name, only_epics, False)


def test_rejects_key_held_by_another_plans_todo_story(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "todo")
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    result = _ingest("mine")

    assert result["ok"] is False
    assert "'TOPO-X'" in result["error"]
    assert "'other'" in result["error"]


def test_rejected_ingest_writes_no_manifest_and_calls_no_provider(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "todo")
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    _ingest("mine")

    assert not (plan_dir / "mine.manifest.json").exists()
    assert provider.calls == []


def test_rejects_case_only_difference(plan_dir, worktree_root, provider, tmp_path):
    _write_other_manifest(plan_dir, "other", "topo-x", "todo")
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    result = _ingest("mine")

    assert result["ok"] is False
    assert "'topo-x'" in result["error"]


def test_accepts_when_other_story_done_and_worktree_gone(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "done")
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    assert _ingest("mine")["ok"] is True


def test_rejects_when_other_story_done_but_worktree_remains(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "done")
    (worktree_root / "TOPO-X").mkdir()
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    assert _ingest("mine")["ok"] is False


def test_reingesting_same_plan_does_not_conflict_with_itself(
    plan_dir, worktree_root, provider, tmp_path
):
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")
    assert _ingest("mine")["ok"] is True
    manifest_path = plan_dir / "mine.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"]["TOPO-X"]["status"] = "in_progress"
    manifest_path.write_text(json.dumps(manifest))

    assert _ingest("mine")["ok"] is True


def test_only_epics_excludes_colliding_key_in_skipped_epic(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "todo")
    clash = {"summary": "clash", "agent_instructions": "Do it.", "key": "TOPO-X"}
    fine = {"summary": "fine", "agent_instructions": "Do it.", "key": "TOPO-Y"}
    _write_plan(
        plan_dir,
        "mine",
        [
            {"summary": "E1", "stories": [clash]},
            {"summary": "E2", "stories": [fine]},
        ],
        tmp_path,
    )

    assert _ingest("mine", only_epics=["E2"])["ok"] is True


def test_keyless_stories_are_never_rejected(plan_dir, worktree_root, provider, tmp_path):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "todo")
    story = {"summary": "TOPO-X", "agent_instructions": "Do it."}
    _write_plan(plan_dir, "mine", [{"summary": "E1", "stories": [story]}], tmp_path)

    assert _ingest("mine")["ok"] is True


def test_error_message_contains_no_filesystem_path(
    plan_dir, worktree_root, provider, tmp_path
):
    _write_other_manifest(plan_dir, "other", "TOPO-X", "todo")
    _plan_with_story(plan_dir, "mine", tmp_path, "TOPO-X")

    error = _ingest("mine")["error"]

    assert str(plan_dir) not in error
    assert str(worktree_root) not in error


def test_schema_rule_documents_key_uniqueness():
    bullets = [
        line
        for line in SCHEMA_MD.read_text().splitlines()
        if "must be unique across plans" in line
    ]

    assert len(bullets) == 1
    assert "ingest_plan" in bullets[0]
