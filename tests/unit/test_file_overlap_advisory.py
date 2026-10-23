"""File-overlap advisory: ingest and the scheduler tick name collisions, never block."""

import json

import pytest

import pipeline.advance as advance_module
from pipeline import server as p
from pipeline import ticketing as pt


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


@pytest.fixture
def notes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: calls.append((plan, msg, kw))
    )
    advance_module._NOTIFIED_FILE_OVERLAPS.clear()
    return calls


def _neighbour(plan_dir, files, status="in_progress"):
    manifest = {"stories": {"N1": {"summary": "n", "status": status, "files": files}}}
    (plan_dir / "other.manifest.json").write_text(json.dumps(manifest))


def _ingest(plan_dir, tmp_path, **story_extra):
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {"repo_root": str(tmp_path), "epics": [{"summary": "E", "stories": [story]}]}
    (plan_dir / "mine.json").write_text(json.dumps(plan))
    return p.ingest_plan("mine")


def _overlap_notes(notes):
    return [n for n in notes if n[2].get("event") == "file_overlap"]


def test_should_notify_and_return_overlap_with_in_flight_neighbour(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, ["a.py", "b.py"])

    result = _ingest(plan_dir, tmp_path, files=["b.py", "c.py"])

    found = _overlap_notes(notes)
    assert len(found) == 1
    assert "S1" in found[0][1] and "other/N1" in found[0][1] and "b.py" in found[0][1]
    assert found[0][2]["story_key"] == "S1"
    assert result["file_overlaps"] == [
        {"story": "S1", "plan": "other", "key": "N1", "files": ["b.py"]}
    ]


def test_should_stay_silent_when_no_files_declared(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, [])

    result = _ingest(plan_dir, tmp_path)

    assert _overlap_notes(notes) == []
    assert result["file_overlaps"] == []


def test_should_stay_silent_when_only_neighbour_declares_files(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, ["a.py"])

    result = _ingest(plan_dir, tmp_path)

    assert _overlap_notes(notes) == []
    assert result["file_overlaps"] == []


def test_should_stay_silent_for_disjoint_files(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, ["a.py"])

    result = _ingest(plan_dir, tmp_path, files=["z.py"])

    assert _overlap_notes(notes) == []
    assert result["file_overlaps"] == []


def test_should_ignore_neighbour_that_is_not_in_flight(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, ["a.py"], status="done")

    result = _ingest(plan_dir, tmp_path, files=["a.py"])

    assert result["file_overlaps"] == []


def test_should_not_change_ok_when_overlap_found(plan_dir, tmp_path, notes):
    _neighbour(plan_dir, ["a.py"])

    result = _ingest(plan_dir, tmp_path, files=["a.py"])

    assert result["ok"] is True
    assert result["file_overlaps"]


def _tick(monkeypatch):
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)

    class _DryRun:
        def _value(self):
            return "dry-run"

    monkeypatch.setattr(advance_module, "PIPELINE_AUTONOMY", _DryRun())
    return advance_module._advance_pipeline_locked_impl("mine")


def _own_manifest(plan_dir, files):
    story = {"summary": "s", "status": "todo", "dependencies": []}
    if files is not None:
        story["files"] = files
    (plan_dir / "mine.manifest.json").write_text(
        json.dumps({"name": "mine", "paused": False, "stories": {"S1": story}})
    )


def test_should_notify_once_across_ticks_for_the_same_neighbour(plan_dir, monkeypatch, notes):
    _neighbour(plan_dir, ["a.py"])
    _own_manifest(plan_dir, ["a.py"])

    first = _tick(monkeypatch)
    _tick(monkeypatch)

    found = _overlap_notes(notes)
    assert len(found) == 1
    assert "other/N1" in found[0][1] and "a.py" in found[0][1]
    assert first["would_dispatch"] == ["S1"]


def test_should_renotify_when_a_new_neighbour_appears(plan_dir, monkeypatch, notes):
    _neighbour(plan_dir, ["a.py"])
    _own_manifest(plan_dir, ["a.py"])
    _tick(monkeypatch)
    manifest = json.loads((plan_dir / "other.manifest.json").read_text())
    manifest["stories"]["N2"] = {"status": "pr_open", "files": ["a.py"]}
    (plan_dir / "other.manifest.json").write_text(json.dumps(manifest))

    _tick(monkeypatch)

    assert len(_overlap_notes(notes)) == 2


def test_should_stay_silent_on_tick_without_declared_files(plan_dir, monkeypatch, notes):
    _neighbour(plan_dir, ["a.py"])
    _own_manifest(plan_dir, None)

    _tick(monkeypatch)

    assert _overlap_notes(notes) == []
