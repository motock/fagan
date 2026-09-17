import json
from pathlib import Path

import pytest

from pipeline.service import PipelineService
from pipeline import server

# Helper to write a JSON file

def write_json(path: Path, data):
    path.write_text(json.dumps(data))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    # Patch server.PLAN_DIR to the tmp_path
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    return tmp_path


def test_list_ingestable_plans_basic(plan_dir):
    write_json(plan_dir / "anagram.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["anagram"]


def test_excludes_non_ingestable_files(plan_dir):
    write_json(plan_dir / "anagram.json", {"epics": {}})
    write_json(plan_dir / "anagram.manifest.json", {"epics": {}})
    write_json(plan_dir / "anagram.decisions.json", {"epics": {}})
    write_json(plan_dir / "anagram.storykey.journal.json", {"epics": {}})
    write_json(plan_dir / "recent_workspaces.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["anagram"]


def test_excludes_file_without_epics(plan_dir):
    write_json(plan_dir / "foo.json", {"not_epics": 1})
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_empty_dir_returns_empty(plan_dir):
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_malformed_json_skipped(plan_dir):
    (plan_dir / "bad.json").write_text("{ not json }")
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_sorted_output(plan_dir):
    write_json(plan_dir / "b.json", {"epics": {}})
    write_json(plan_dir / "a.json", {"epics": {}})
    write_json(plan_dir / "c.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["a", "b", "c"]

