"""run_triage_sweep must not clobber manifest writes made while it runs.

The sweep reads the manifest once, spends minutes in LLM rulings, then
persists. Anything written to the file in between must survive; only the
mutations the sweep itself made may be applied. Only the overlord LLM
boundary (``rule_on_story``) is stubbed.
"""

from __future__ import annotations

import json

import pytest

from pipeline import triage as triage_mod

PLAN_NAME = "stale1"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def manifest_path(plan_dir):
    return plan_dir / f"{PLAN_NAME}.manifest.json"


def _parked(**extra):
    story = {
        "status": "parked",
        "parked_reason": "stuck",
        "worktree": "",
        "triage_attempts": 0,
        "triage_actions": [],
    }
    story.update(extra)
    return story


def _write(path, manifest):
    path.write_text(json.dumps(manifest, indent=2))


def _read(path):
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
    monkeypatch.setattr(triage_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(triage_mod, "classify_repo_health", lambda *a, **k: [])
    monkeypatch.setattr(triage_mod, "collect_triage_evidence", lambda *a, **k: "")


def _stub_ruling(monkeypatch, during_rule=None):
    def _rule(plan_name, story_key, story, evidence):
        if during_rule:
            during_rule(story_key)
        return {"action": "park_for_human", "rationale": "synthetic"}

    monkeypatch.setattr(triage_mod, "rule_on_story", _rule)


def test_should_keep_disk_write_made_by_ruling_executor_for_triaged_story(
    monkeypatch, manifest_path
):
    _write(manifest_path, {"stories": {"A": _parked()}})
    _stub_ruling(monkeypatch)

    def _apply(plan_name, key, story, ruling, manifest, path):
        # An executor helper persists its own change straight to disk.
        on_disk = _read(path)
        on_disk["stories"][key]["helper_note"] = "written-by-executor"
        _write(path, on_disk)
        story["triage_attempts"] = 1
        return ruling["action"]

    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _apply)

    result = triage_mod.run_triage_sweep(PLAN_NAME)

    assert result["ok"] is True
    persisted = _read(manifest_path)["stories"]["A"]
    assert persisted["helper_note"] == "written-by-executor"
    assert persisted["triage_attempts"] == 1


def test_should_keep_external_write_to_untriaged_story_made_during_ruling(
    monkeypatch, manifest_path
):
    _write(manifest_path, {"stories": {"A": _parked(), "B": _parked(triage_attempts=2, rework_attempts=3)}})

    def _external_edit(key):
        on_disk = _read(manifest_path)
        on_disk["stories"]["B"]["status"] = "todo"
        on_disk["stories"]["B"]["rework_attempts"] = 0
        _write(manifest_path, on_disk)

    _stub_ruling(monkeypatch, _external_edit)

    triage_mod.run_triage_sweep(PLAN_NAME)

    persisted = _read(manifest_path)["stories"]
    assert persisted["B"]["status"] == "todo"
    assert persisted["B"]["rework_attempts"] == 0
    assert persisted["A"]["triage_attempts"] == 1


def test_should_not_resurrect_story_dropped_during_sweep(monkeypatch, manifest_path):
    _write(manifest_path, {"stories": {"A": _parked(), "B": _parked(triage_attempts=2)}})

    def _reingest_drops_a_and_b(key):
        on_disk = _read(manifest_path)
        del on_disk["stories"]["A"]
        del on_disk["stories"]["B"]
        _write(manifest_path, on_disk)

    _stub_ruling(monkeypatch, _reingest_drops_a_and_b)

    triage_mod.run_triage_sweep(PLAN_NAME)

    assert _read(manifest_path)["stories"] == {}


def test_should_persist_split_children_and_counter_added_by_sweep(
    monkeypatch, manifest_path
):
    _write(manifest_path, {"stories": {"A": _parked()}, "triage_created_stories": 0})
    _stub_ruling(monkeypatch)

    def _apply(plan_name, key, story, ruling, manifest, path):
        manifest["stories"]["A-split-1"] = {"status": "todo", "summary": "child"}
        manifest["triage_created_stories"] = 1
        return ruling["action"]

    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _apply)

    triage_mod.run_triage_sweep(PLAN_NAME)

    persisted = _read(manifest_path)
    assert persisted["stories"]["A-split-1"]["summary"] == "child"
    assert persisted["triage_created_stories"] == 1


def test_should_strip_patch_acceptance_sentinel_on_write(monkeypatch, manifest_path):
    _write(manifest_path, {"stories": {"A": _parked()}})
    _stub_ruling(monkeypatch)

    def _apply(plan_name, key, story, ruling, manifest, path):
        story["_patch_acceptance_recorded"] = True
        return ruling["action"]

    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _apply)

    triage_mod.run_triage_sweep(PLAN_NAME)

    assert "_patch_acceptance_recorded" not in _read(manifest_path)["stories"]["A"]


def test_should_not_write_when_there_are_no_candidates(monkeypatch, manifest_path):
    manifest = {"stories": {"A": {"status": "done"}}}
    _write(manifest_path, manifest)
    before = manifest_path.stat().st_mtime_ns
    writes = []
    monkeypatch.setattr(triage_mod, "_atomic_write_json", lambda *a: writes.append(a))

    result = triage_mod.run_triage_sweep(PLAN_NAME)

    assert result == {"ok": True, "triaged": []}
    assert writes == []
    assert manifest_path.stat().st_mtime_ns == before


def test_should_fail_open_when_manifest_unreadable_at_write(monkeypatch, manifest_path):
    _write(manifest_path, {"stories": {"A": _parked()}})

    def _corrupt(key):
        manifest_path.write_text("{not json")

    _stub_ruling(monkeypatch, _corrupt)

    result = triage_mod.run_triage_sweep(PLAN_NAME)

    assert result["ok"] is False
