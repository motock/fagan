"""advance reads every plan's manifest in bulk through the record store.

Two per-tick reads used to bypass the store and go straight to disk:
``_count_on_device_in_progress_agents`` globbed ``PLAN_DIR`` and
``_advance_pipeline_locked_impl`` loaded neighbour plans through
``cross_plan.load_manifests``. Both now read
``_store.records().get_manifests("default")``, so a fake store alone must be
enough to drive them.
"""

from __future__ import annotations

import json
import os

import pipeline.advance as advance_module
from pipeline import server as p


class _FakeRecordStore:
    """A store whose only useful surface is the bulk manifest read."""

    def __init__(self, manifests, manifest_path=None):
        self._manifests = manifests
        self._manifest_path = manifest_path

    def manifest_path(self, plan_name):
        return self._manifest_path

    def get_manifest(self, plan_name):
        return self._manifests[plan_name]

    def records(self):
        return self

    def get_manifests(self, namespace):
        assert namespace == "default"
        return self._manifests


def test_should_count_on_device_agents_from_the_record_store(tmp_path, monkeypatch):
    empty_plan_dir = tmp_path / "plans"
    empty_plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", empty_plan_dir)

    live = {"stories": {"s1": {"status": "in_progress", "pid": os.getpid()}}}
    finished = {"stories": {"s2": {"status": "done"}}}
    monkeypatch.setattr(
        advance_module,
        "_store",
        _FakeRecordStore({"current": finished, "other": live}),
    )

    assert advance_module._count_on_device_in_progress_agents() == 1


def test_should_skip_the_current_plan_when_loading_neighbours(tmp_path, monkeypatch):
    manifest_path = tmp_path / "current.manifest.json"
    current = {
        "name": "current",
        "paused": False,
        "stories": {"S1": {"summary": "s", "status": "todo", "dependencies": []}},
    }
    neighbour = {"stories": {"N1": {"status": "pr_open", "files": ["a.py"]}}}
    manifest_path.write_text(json.dumps(current))
    monkeypatch.setattr(
        advance_module,
        "_store",
        _FakeRecordStore(
            {"current": current, "neighbour": neighbour}, manifest_path=manifest_path
        ),
    )

    captured = {}

    def _capture(plan_name, stories, ready, neighbours):
        captured["plan_name"] = plan_name
        captured["neighbours"] = neighbours

    monkeypatch.setattr(advance_module, "_warn_file_overlaps", _capture)
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)

    class _DryRun:
        def _value(self):
            return "dry-run"

    monkeypatch.setattr(advance_module, "PIPELINE_AUTONOMY", _DryRun())

    advance_module._advance_pipeline_locked_impl("current")

    assert captured["plan_name"] == "current"
    assert set(captured["neighbours"]) == {"neighbour"}
