import json
from pathlib import Path

import pytest

from pipeline.service import PipelineService, PLAN_DIR

# Helper to monkeypatch PLAN_DIR to a tmp_path
@pytest.fixture
def service(tmp_path: Path):
    # monkeypatch pipeline.server.PLAN_DIR
    import pipeline.server
    pipeline.server.PLAN_DIR = tmp_path
    return PipelineService()

# Test only anagram.json with epics

def test_single_valid_plan(service):
    anagram_path = PLAN_DIR / "anagram.json"
    anagram_path.write_text(json.dumps({"epics": {}, "name": "anagram"}))
    assert service.list_ingestable_plans() == ["anagram"]

# Test exclusion of other files

def test_exclusion_of_other_files(service):
    base = PLAN_DIR
    base.mkdir(parents=True, exist_ok=True)
    # create valid anagram.json
    (base / "anagram.json").write_text(json.dumps({"epics": {}, "name": "anagram"}))
    # create other files
    (base / "anagram.manifest.json").write_text(json.dumps({"epics": {}, "name": "anagram"}))
    (base / "anagram.decisions.json").write_text(json.dumps({"epics": {}, "name": "anagram"}))
    (base / "anagram.storykey.journal.json").write_text(json.dumps({"epics": {}, "name": "anagram"}))
    # create recent_workspaces.json without epics
    (base / "recent_workspaces.json").write_text(json.dumps({"foo": 1}))
    # create active_workspace.json without epics
    (base / "active_workspace.json").write_text(json.dumps({"bar": 2}))
    assert service.list_ingestable_plans() == ["anagram"]

# Test empty dir

def test_empty_dir(service):
    assert service.list_ingestable_plans() == []

# Test malformed content

def test_malformed_content(service):
    (PLAN_DIR / "bad.json").write_text("{not json")
    assert service.list_ingestable_plans() == []

# Test non-object JSON

def test_non_object_json(service):
    (PLAN_DIR / "gamma.json").write_text(json.dumps(5))
    (PLAN_DIR / "delta.json").write_text(json.dumps(None))
    assert service.list_ingestable_plans() == []

# Test sorting

def test_sorting(service):
    base = PLAN_DIR
    base.mkdir(parents=True, exist_ok=True)
    (base / "zeta.json").write_text(json.dumps({"epics": {}}))
    (base / "alpha.json").write_text(json.dumps({"epics": {}}))
    (base / "mid.json").write_text(json.dumps({"epics": {}}))
    assert service.list_ingestable_plans() == ["alpha", "mid", "zeta"]

# Test state across calls

def test_state_across_calls(service):
    base = PLAN_DIR
    base.mkdir(parents=True, exist_ok=True)
    (base / "anagram.json").write_text(json.dumps({"epics": {}}))
    assert service.list_ingestable_plans() == ["anagram"]
    (base / "beta.json").write_text(json.dumps({"epics": {}}))
    assert service.list_ingestable_plans() == ["anagram", "beta"]
