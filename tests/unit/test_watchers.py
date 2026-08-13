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
