"""Neighbour manifest scans read through the record store, not the plan dir.

Two callers used to glob ``PLAN_DIR`` themselves for every other plan's
manifest: ``PipelineService.list_ready_stories`` and the dispatch
key-collision check in ``pipeline/dispatch.py``. Both now read
``_store.records().get_manifests("default")``, so a fake store alone must be
enough to drive them and an empty plan directory must not hide a neighbour.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from pipeline import server as p


class _FakeRecordStore:
    """A store whose only useful surface is the bulk manifest read."""

    def __init__(self, manifests, manifest_path=None):
        self._manifests = manifests
        self._manifest_path = manifest_path
        self.requested_namespaces = []

    def audit_log(self):
        """The file audit log, which is what the real store serves under file."""
        from pipeline.audit_file import FileAuditLog

        return FileAuditLog()

    def manifest_path(self, plan_name):
        return self._manifest_path

    def get_manifest(self, plan_name):
        return self._manifests[plan_name]

    def records(self):
        return self

    def list_plans(self, namespace):
        assert namespace == "default"
        return sorted(self._manifests)

    def get_manifests(self, namespace):
        self.requested_namespaces.append(namespace)
        assert namespace == "default"
        return dict(self._manifests)

    def put_manifest(self, namespace, plan_name, manifest):
        # dispatch_story persists its own claim/brief update through the
        # record store (ADR 0012); keep the fake's view in step with it.
        assert namespace == "default"
        self._manifests[plan_name] = manifest

    @contextlib.contextmanager
    def transaction(self, plan_name):
        yield True


def _story(status="todo", **extra):
    story = {"summary": "s", "agent_instructions": "Build.", "status": status,
             "dependencies": []}
    story.update(extra)
    return story


def _manifest(stories):
    return {"epics": {}, "stories": stories}


@pytest.fixture
def empty_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _install(monkeypatch, manifests, plan_dir, plan_name="current"):
    """Fake store holding *manifests*; the plan dir holds only ``plan_name``."""
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifests[plan_name]))
    fake = _FakeRecordStore(manifests, manifest_path=manifest_path)
    monkeypatch.setattr(p, "_store", fake)
    return fake


def test_should_block_a_ready_story_on_a_neighbour_gap_from_the_record_store(
    empty_plan_dir, monkeypatch
):
    """The neighbour exists only in the store, so a dir glob cannot see it."""
    fake = _install(
        monkeypatch,
        {
            "current": _manifest(
                {"S1": _story(blocked_by=["neighbour:N1"])}
            ),
            "neighbour": _manifest({"N1": _story()}),
        },
        empty_plan_dir,
    )

    ready = p.PipelineService().list_ready_stories("current")

    assert ready == []
    assert fake.requested_namespaces
    assert set(fake.requested_namespaces) == {"default"}


def test_should_exclude_the_plan_under_query_from_its_neighbours(
    empty_plan_dir, monkeypatch
):
    """A self-reference must never resolve against the plan's own manifest.

    ``current`` is asked to wait on ``current:DONE``. If its own manifest were
    counted as a neighbour, that reference would resolve and S1 would look
    ready; because the plan under query is excluded, the reference stays a gap.
    """
    _install(
        monkeypatch,
        {
            "current": _manifest(
                {
                    "S1": _story(blocked_by=["current:DONE"]),
                    "DONE": _story(status="done"),
                }
            ),
            "neighbour": _manifest({"N1": _story(status="done")}),
        },
        empty_plan_dir,
    )

    ready = p.PipelineService().list_ready_stories("current")

    assert ready == []


def test_should_return_a_ready_story_when_the_store_reports_no_neighbours(
    empty_plan_dir, monkeypatch
):
    """Boundary: an empty bulk read means ready, not blocked."""
    fake = _install(
        monkeypatch,
        {"current": _manifest({"S1": _story()})},
        empty_plan_dir,
    )

    ready = p.PipelineService().list_ready_stories("current")

    assert [s["key"] for s in ready] == ["S1"]
    assert fake.requested_namespaces == ["default"]


def test_should_refuse_dispatch_on_a_key_held_by_a_neighbour_from_the_record_store(
    empty_plan_dir, monkeypatch
):
    """The colliding key lives only in the store, not in any plan file."""
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: notes.append((plan, msg, kw))
    )
    _install(
        monkeypatch,
        {
            "current": _manifest({"TOPO-X": _story()}),
            "neighbour": _manifest({"TOPO-X": _story(status="in_progress")}),
        },
        empty_plan_dir,
    )

    result = p.dispatch_story("current", "TOPO-X")

    assert result["ok"] is False
    assert result["error"] == "story key collides with another plan"
    assert result["plan"] == "neighbour"
    assert [n[2]["event"] for n in notes] == ["story_key_conflict"]
