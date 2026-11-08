"""The checkpoint tool reads the plan manifest through the record store.

``pipeline.checkpoint._checkpoint_impl`` used to load
``PLAN_DIR / "<plan>.manifest.json"`` itself, so under
``PIPELINE_STORE=sqlite`` a checkpoint taken on this host could not see the
manifest the database holds. It now reads
``_store.records().get_manifest("default", plan_name)`` — mirroring
``pipeline.cross_plan.neighbour_manifests`` — and keeps the legacy
plan-directory read as the fallback for a store with no record view, so
in-process store stand-ins keep working unchanged.
"""

from __future__ import annotations

import json

import pytest

from pipeline import checkpoint
from pipeline import server as p


class _FakeRecordStore:
    """A store whose only useful surface is the single-manifest record read."""

    def __init__(self, manifests):
        self._manifests = manifests
        self.requested = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        self.requested.append((namespace, plan_name))
        return self._manifests.get(plan_name)

    def get_manifests(self, namespace):
        assert namespace == "default"
        return dict(self._manifests)


class _LegacyStore:
    """A store with no record view at all (an in-process stand-in)."""

    def __init__(self, plan_dir):
        self._plan_dir = plan_dir

    def manifest_path(self, plan_name):
        return self._plan_dir / f"{plan_name}.manifest.json"


@pytest.fixture
def seams(monkeypatch, tmp_path):
    """Isolate PLAN_DIR and the two side effects the checkpoint performs."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)

    committed = []

    def fake_commit_wip(worktree, story_key, step, **kwargs):
        committed.append((worktree, story_key, step))
        return "deadbeef"

    journal = []
    monkeypatch.setattr(checkpoint, "_commit_wip", fake_commit_wip)
    monkeypatch.setattr(
        checkpoint, "_append_journal", lambda plan, key, record: journal.append((plan, key, record))
    )
    return {"plan_dir": plan_dir, "committed": committed, "journal": journal}


def _manifest(**story_extra):
    story = {"status": "todo", "worktree": "/tmp/wt", "summary": "s"}
    story.update(story_extra)
    return {"epics": {}, "stories": {"story-a": story}}


def test_should_read_the_manifest_from_the_record_store_not_the_plan_dir(seams, monkeypatch):
    """Nothing is on disk; the store alone must satisfy the checkpoint."""
    fake = _FakeRecordStore({"current": _manifest()})
    monkeypatch.setattr(p, "_store", fake)

    result = checkpoint._checkpoint_impl("current", "story-a", "step-1", "did it")

    assert result["ok"] is True
    assert fake.requested == [("default", "current")]
    assert seams["committed"] == [("/tmp/wt", "story-a", "step-1")]
    assert seams["journal"][0][:2] == ("current", "story-a")
    assert not (seams["plan_dir"] / "current.manifest.json").exists()


def test_should_report_an_unknown_story_that_only_the_store_knows_about(seams, monkeypatch):
    """The store's manifest is what decides whether the story exists."""
    monkeypatch.setattr(p, "_store", _FakeRecordStore({"current": _manifest()}))

    result = checkpoint._checkpoint_impl("current", "story-zzz", "step-1", "s")

    assert result == {"ok": False, "error": "No such story story-zzz"}
    assert seams["committed"] == []


def test_should_fall_back_to_the_plan_dir_for_a_store_with_no_record_view(seams, monkeypatch):
    """An in-process stand-in without ``records()`` keeps the legacy read."""
    (seams["plan_dir"] / "current.manifest.json").write_text(json.dumps(_manifest()))
    monkeypatch.setattr(p, "_store", _LegacyStore(seams["plan_dir"]))

    result = checkpoint._checkpoint_impl("current", "story-a", "step-1", "s")

    assert result["ok"] is True
    assert seams["committed"] == [("/tmp/wt", "story-a", "step-1")]


def test_should_fall_back_to_the_plan_dir_when_the_store_has_no_record(seams, monkeypatch):
    """A record view that does not know the plan must not hide the legacy file."""
    (seams["plan_dir"] / "current.manifest.json").write_text(json.dumps(_manifest()))
    fake = _FakeRecordStore({})
    monkeypatch.setattr(p, "_store", fake)

    result = checkpoint._checkpoint_impl("current", "story-a", "step-1", "s")

    assert result["ok"] is True
    assert fake.requested == [("default", "current")]
