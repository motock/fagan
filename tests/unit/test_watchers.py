"""Tests for pipeline.watchers.scan_done_markers.

These tests are written FIRST (TDD). The implementation module
``pipeline.watchers`` does not exist yet, so this suite is expected to be RED
until a later dispatch implements it.

The watcher's only job is to convert ``.agent_done`` marker files inside
in-progress stories' worktrees into ``agent_done`` events on the EventBus,
then rename the marker to ``.agent_done.consumed`` so the completion is not
republished on the next scan. It must never run tests, grade stories, mutate
the manifest, or invoke subprocess.
"""

import json
import logging
import subprocess

import pytest

from pipeline import watchers

PLAN = "plan-42"


class FakeBus:
    """A minimal bus that records every published event."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def _marker_path(worktree):
    return worktree / ".agent_done"


def _consumed_path(worktree):
    return worktree / ".agent_done.consumed"


def _write_marker(worktree, payload):
    _marker_path(worktree).write_text(json.dumps(payload))


def _manifest(stories):
    """Build a manifest dict with the given stories mapping."""
    return {"plan": PLAN, "stories": dict(stories)}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_in_progress_marker_publishes_one_event_with_marker_payload(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    payload = {"exit_code": 0, "note": "done"}
    _write_marker(worktree, payload)

    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert len(bus.published) == 1
    assert result == bus.published
    ev = bus.published[0]
    assert ev["type"] == "agent_done"
    assert ev["plan"] == PLAN
    assert ev["story_key"] == "S-1"
    assert ev["payload"] == payload


def test_marker_renamed_to_consumed_after_publishing(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})
    bus = FakeBus()

    watchers.scan_done_markers(manifest, PLAN, bus)

    assert not _marker_path(worktree).exists()
    assert _consumed_path(worktree).exists()


def test_second_scan_after_rename_publishes_nothing(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})
    bus = FakeBus()

    watchers.scan_done_markers(manifest, PLAN, bus)
    # Second scan: marker is now .agent_done.consumed, so nothing to publish.
    second = watchers.scan_done_markers(manifest, PLAN, bus)

    assert second == []
    assert len(bus.published) == 1  # still only the first event


# ---------------------------------------------------------------------------
# Negative / skip cases
# ---------------------------------------------------------------------------

def test_non_in_progress_status_skipped_even_with_marker(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _manifest({"S-1": {"status": "done", "worktree": str(worktree)}})
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert result == []
    assert bus.published == []
    # marker untouched
    assert _marker_path(worktree).exists()
    assert not _consumed_path(worktree).exists()


def test_story_without_worktree_key_skipped_no_raise(tmp_path):
    # A marker exists somewhere but the story has no worktree key at all.
    manifest = _manifest({"S-1": {"status": "in_progress"}})
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert result == []
    assert bus.published == []


def test_missing_worktree_directory_skipped_silently(tmp_path):
    # worktree path does not exist on disk.
    missing = tmp_path / "does-not-exist"
    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(missing)}})
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert result == []
    assert bus.published == []


def test_malformed_marker_skipped_not_renamed_no_raise(tmp_path, caplog):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _marker_path(worktree).write_text("{ this is not valid json")

    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})
    bus = FakeBus()

    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert result == []
    assert bus.published == []
    # malformed marker must remain for diagnosis, NOT renamed
    assert _marker_path(worktree).exists()
    assert not _consumed_path(worktree).exists()
    # a WARNING was logged
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Non-mutation / no-side-effect guarantees
# ---------------------------------------------------------------------------

def test_manifest_not_mutated_by_scan(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _manifest(
        {"S-1": {"status": "in_progress", "worktree": str(worktree)}}
    )
    before = json.loads(json.dumps(manifest))
    bus = FakeBus()

    watchers.scan_done_markers(manifest, PLAN, bus)

    assert manifest == before


def test_scan_does_not_invoke_subprocess(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})
    bus = FakeBus()

    def _boom(*args, **kwargs):
        pytest.fail("scan_done_markers must not invoke subprocess.run")

    monkeypatch.setattr(subprocess, "run", _boom)
    # Also guard the lower-level call used by subprocess.run on some platforms.
    monkeypatch.setattr(subprocess, "Popen", _boom)

    watchers.scan_done_markers(manifest, PLAN, bus)


# ---------------------------------------------------------------------------
# Boundary: empty manifest, multiple stories
# ---------------------------------------------------------------------------

def test_empty_stories_manifest_returns_empty(tmp_path):
    manifest = _manifest({})
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert result == []
    assert bus.published == []


def test_multiple_stories_only_in_progress_with_marker_publish(tmp_path):
    wt_done = tmp_path / "done"
    wt_done.mkdir()
    _write_marker(wt_done, {"exit_code": 0})

    wt_pending = tmp_path / "pending"
    wt_pending.mkdir()
    # no marker in wt_pending

    wt_other = tmp_path / "other"
    wt_other.mkdir()
    _write_marker(wt_other, {"exit_code": 0})

    manifest = _manifest(
        {
            "S-done": {"status": "in_progress", "worktree": str(wt_done)},
            "S-pending": {"status": "in_progress", "worktree": str(wt_pending)},
            "S-other": {"status": "review", "worktree": str(wt_other)},
        }
    )
    bus = FakeBus()

    result = watchers.scan_done_markers(manifest, PLAN, bus)

    assert len(result) == 1
    assert result[0]["story_key"] == "S-done"
    assert result[0]["payload"] == {"exit_code": 0}


# ---------------------------------------------------------------------------
# Import hygiene: must not import from pipeline.server (circular import)
# ---------------------------------------------------------------------------

def test_watchers_does_not_import_pipeline_server():
    import inspect

    import pipeline.watchers as w

    src = inspect.getsource(w)
    assert "pipeline.server" not in src
    assert "from pipeline.server" not in src
    assert "import pipeline.server" not in src


def test_watchers_imports_make_event_from_events():
    import inspect

    import pipeline.watchers as w

    src = inspect.getsource(w)
    assert "make_event" in src
    assert ".events" in src or "pipeline.events" in src


# ===========================================================================
# scan_all_plans: sweep across every ingested plan in PLAN_DIR.
#
# These tests are written FIRST (TDD).  ``scan_all_plans`` does not exist yet,
# so this section is expected to be RED until a later dispatch implements it.
# ===========================================================================

import inspect

from pipeline import paths


def _write_manifest(plan_name, manifest, plan_dir):
    """Write a manifest dict to <plan_dir>/<plan_name>.manifest.json."""
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _manifest_with_marker(plan_name, worktree):
    """A manifest for ``plan_name`` with one in_progress story holding a marker."""
    return {
        "plan": plan_name,
        "stories": {
            "S-1": {"status": "in_progress", "worktree": str(worktree)},
        },
    }


def _make_plan_with_marker(plan_dir, plan_name, payload=None):
    """Create a plan dir + worktree + marker + manifest in ``plan_dir``."""
    worktree = plan_dir / f"{plan_name}-wt"
    worktree.mkdir()
    _write_marker(worktree, payload if payload is not None else {"exit_code": 0})
    manifest = _manifest_with_marker(plan_name, worktree)
    _write_manifest(plan_name, manifest, plan_dir)
    return worktree


def test_scan_all_plans_two_plans_each_one_marker_publishes_two_events(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _make_plan_with_marker(tmp_path, "alpha", {"exit_code": 0})
    _make_plan_with_marker(tmp_path, "beta", {"exit_code": 1})
    bus = FakeBus()

    result = watchers.scan_all_plans(bus)

    assert len(result) == 2
    assert len(bus.published) == 2
    assert result == bus.published


def test_scan_all_plans_event_plan_name_is_filename_without_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _make_plan_with_marker(tmp_path, "plan-7", {"exit_code": 0})
    bus = FakeBus()

    result = watchers.scan_all_plans(bus)

    assert len(result) == 1
    assert result[0]["plan"] == "plan-7"
    # The plan name must be the bare filename, not the full path / not suffixed.
    assert ".manifest.json" not in result[0]["plan"]


def test_scan_all_plans_skips_paused_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    # Paused plan with a marker that WOULD publish if scanned.
    paused_wt = _make_plan_with_marker(tmp_path, "paused-plan", {"exit_code": 0})
    # Flip the paused flag on its manifest.
    paused_manifest = _manifest_with_marker("paused-plan", paused_wt)
    paused_manifest["paused"] = True
    _write_manifest("paused-plan", paused_manifest, tmp_path)
    # A second, active plan that should still publish.
    _make_plan_with_marker(tmp_path, "active-plan", {"exit_code": 0})
    bus = FakeBus()

    result = watchers.scan_all_plans(bus)

    assert len(result) == 1
    assert result[0]["plan"] == "active-plan"
    assert all(ev["plan"] != "paused-plan" for ev in result)


def test_scan_all_plans_skips_unparseable_manifest_and_continues(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    # A garbage manifest file.
    (tmp_path / "broken.manifest.json").write_text("{ not valid json")
    # A valid plan alongside it.
    _make_plan_with_marker(tmp_path, "good-plan", {"exit_code": 0})
    bus = FakeBus()

    with caplog.at_level(logging.WARNING):
        result = watchers.scan_all_plans(bus)

    assert len(result) == 1
    assert result[0]["plan"] == "good-plan"
    # The broken plan must have logged a WARNING.
    assert any(
        "broken" in record.getMessage() and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_scan_all_plans_missing_manifest_file_skipped(tmp_path, monkeypatch, caplog):
    """A manifest path that disappears between glob and read must not crash."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    # Create then immediately remove a manifest to simulate a missing file.
    path = tmp_path / "ghost.manifest.json"
    path.write_text(json.dumps({"plan": "ghost", "stories": {}}))
    path.unlink()
    _make_plan_with_marker(tmp_path, "real-plan", {"exit_code": 0})
    bus = FakeBus()

    with caplog.at_level(logging.WARNING):
        result = watchers.scan_all_plans(bus)

    assert len(result) == 1
    assert result[0]["plan"] == "real-plan"


def test_scan_all_plans_empty_plan_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    bus = FakeBus()

    result = watchers.scan_all_plans(bus)

    assert result == []
    assert bus.published == []


def test_scan_all_plans_returns_flat_concatenation(tmp_path, monkeypatch):
    """Multiple plans each contributing multiple events flatten into one list."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)

    # Plan A: two in_progress stories, each with a marker.
    wt_a1 = tmp_path / "a-wt1"
    wt_a1.mkdir()
    _write_marker(wt_a1, {"exit_code": 0})
    wt_a2 = tmp_path / "a-wt2"
    wt_a2.mkdir()
    _write_marker(wt_a2, {"exit_code": 1})
    _write_manifest(
        "planA",
        {
            "plan": "planA",
            "stories": {
                "A-1": {"status": "in_progress", "worktree": str(wt_a1)},
                "A-2": {"status": "in_progress", "worktree": str(wt_a2)},
            },
        },
        tmp_path,
    )

    # Plan B: one in_progress story with a marker.
    wt_b1 = tmp_path / "b-wt1"
    wt_b1.mkdir()
    _write_marker(wt_b1, {"exit_code": 2})
    _write_manifest(
        "planB",
        {
            "plan": "planB",
            "stories": {
                "B-1": {"status": "in_progress", "worktree": str(wt_b1)},
            },
        },
        tmp_path,
    )

    bus = FakeBus()
    result = watchers.scan_all_plans(bus)

    assert len(result) == 3
    assert len(bus.published) == 3
    # Flat list, not nested per-plan.
    assert all(isinstance(ev, dict) for ev in result)
    plans = sorted(ev["plan"] for ev in result)
    assert plans == ["planA", "planA", "planB"]


def test_scan_all_plans_never_invokes_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _make_plan_with_marker(tmp_path, "p", {"exit_code": 0})
    bus = FakeBus()

    def _boom(*args, **kwargs):
        pytest.fail("scan_all_plans must not invoke subprocess.run")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    watchers.scan_all_plans(bus)


def test_scan_all_plans_does_not_import_pipeline_server():
    import pipeline.watchers as w

    src = inspect.getsource(w)
    assert "pipeline.server" not in src
    assert "from pipeline.server" not in src
    assert "import pipeline.server" not in src


def test_scan_all_plans_imports_PLAN_DIR_from_paths():
    import pipeline.watchers as w

    src = inspect.getsource(w)
    # Must reference PLAN_DIR and import it from .paths (not pipeline.server).
    assert "PLAN_DIR" in src
    assert ".paths" in src or "from pipeline.paths" in src


def test_scan_all_plans_uses_removesuffix_idiom():
    """Plan names must be derived with name.removesuffix('.manifest.json')."""
    import pipeline.watchers as w

    src = inspect.getsource(w)
    assert "removesuffix" in src
    assert ".manifest.json" in src


def test_scan_all_plans_calls_scan_done_markers(tmp_path, monkeypatch):
    """scan_all_plans must delegate to scan_done_markers per plan."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _make_plan_with_marker(tmp_path, "delegated", {"exit_code": 0})
    bus = FakeBus()

    calls = []
    original = watchers.scan_done_markers

    def spy(manifest, plan, b):
        calls.append((plan, manifest.get("plan")))
        return original(manifest, plan, b)

    monkeypatch.setattr(watchers, "scan_done_markers", spy)
    result = watchers.scan_all_plans(bus)

    assert len(calls) == 1
    assert calls[0] == ("delegated", "delegated")
    assert len(result) == 1


def test_scan_all_plans_does_not_mutate_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _make_plan_with_marker(tmp_path, "m", {"exit_code": 0})
    bus = FakeBus()

    before = json.loads((tmp_path / "m.manifest.json").read_text())
    watchers.scan_all_plans(bus)
    after = json.loads((tmp_path / "m.manifest.json").read_text())

    assert before == after
