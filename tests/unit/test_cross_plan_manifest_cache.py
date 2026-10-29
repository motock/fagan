"""Tests for the file-stamp manifest cache in ``pipeline.cross_plan``.

``load_manifests`` used to re-read and re-parse every ``*.manifest.json`` on
every call; ``pipeline.advance``/``dispatch``/``service``/``ingest`` call it
per plan advance and ``watchers.scan_all_plans`` calls it about once a second
in the scheduler daemon. A long-lived process therefore re-parsed files that
had not changed.

``read_manifest_cached`` keys a module-level cache on the resolved path and
stores ``(st_mtime_ns, st_size, parsed)``, so a read that finds the same stamp
returns the already-parsed dict. These tests pin the cache contract:
reuse on an unchanged stamp, re-parse on a changed mtime or size, eviction on
delete, and no caching of unreadable or non-object manifests.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

# pipeline.server is imported explicitly first; import order is not otherwise
# required for cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401
from pipeline import cross_plan, paths, watchers


@pytest.fixture(autouse=True)
def _clean_manifest_cache():
    """Keep the module-level cache from leaking between tests."""
    cross_plan.clear_manifest_cache()
    yield
    cross_plan.clear_manifest_cache()


def _write_manifest(plan_dir: Path, name: str, payload: dict) -> Path:
    path = plan_dir / f"{name}.manifest.json"
    path.write_text(json.dumps(payload))
    return path


def _bump_mtime(path: Path, delta_ns: int = 10_000_000) -> None:
    """Push ``st_mtime_ns`` forward so the cached stamp is definitely stale."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + delta_ns))


class _CountingLoads:
    """Count ``json.loads`` calls made through ``pipeline.cross_plan.json``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, real=json.loads):
        self.calls = 0
        self._real = real
        monkeypatch.setattr(cross_plan.json, "loads", self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._real(*args, **kwargs)


def test_two_calls_without_a_file_change_parse_once(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "alpha", {"stories": {"S1": {"status": "done"}}})
    loads = _CountingLoads(monkeypatch)

    first = cross_plan.load_manifests(tmp_path)
    second = cross_plan.load_manifests(tmp_path)

    assert loads.calls == 1
    assert first == second
    assert first["alpha"]["stories"]["S1"]["status"] == "done"


def test_read_manifest_cached_returns_the_same_object_on_an_unchanged_stamp(tmp_path, monkeypatch):
    path = _write_manifest(tmp_path, "alpha", {"stories": {}})
    loads = _CountingLoads(monkeypatch)

    first = cross_plan.read_manifest_cached(path)
    second = cross_plan.read_manifest_cached(path)

    assert loads.calls == 1
    # The cached dict is shared, not copied: callers must only read it.
    assert first is second


def test_bumping_the_mtime_reparses_and_returns_the_new_content(tmp_path, monkeypatch):
    path = _write_manifest(tmp_path, "alpha", {"stories": {"S1": {"status": "in_progress"}}})
    loads = _CountingLoads(monkeypatch)
    assert cross_plan.read_manifest_cached(path)["stories"]["S1"]["status"] == "in_progress"

    path.write_text(json.dumps({"stories": {"S1": {"status": "done"}}}))
    _bump_mtime(path)

    refreshed = cross_plan.read_manifest_cached(path)

    assert loads.calls == 2
    assert refreshed["stories"]["S1"]["status"] == "done"


def test_a_same_mtime_write_with_a_different_size_reparses(tmp_path, monkeypatch):
    path = _write_manifest(tmp_path, "alpha", {"stories": {}})
    loads = _CountingLoads(monkeypatch)
    original = path.stat()
    assert cross_plan.read_manifest_cached(path) == {"stories": {}}

    # Same mtime, materially different size: a coarse-granularity filesystem
    # would otherwise serve the stale parse forever.
    path.write_text(json.dumps({"stories": {}, "padding": "x" * 512}))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    refreshed = cross_plan.read_manifest_cached(path)

    assert loads.calls == 2
    assert refreshed["padding"] == "x" * 512


def test_a_deleted_manifest_leaves_load_manifests_and_the_cache(tmp_path, monkeypatch):
    keep = _write_manifest(tmp_path, "keep", {"stories": {}})
    gone = _write_manifest(tmp_path, "gone", {"stories": {}})
    _CountingLoads(monkeypatch)
    assert set(cross_plan.load_manifests(tmp_path)) == {"keep", "gone"}

    gone.unlink()

    assert set(cross_plan.load_manifests(tmp_path)) == {"keep"}
    # A direct read of the removed path reports absence and drops the entry.
    assert cross_plan.read_manifest_cached(gone) is None
    assert cross_plan.read_manifest_cached(keep) is not None


def test_a_malformed_manifest_is_skipped_not_cached_and_picked_up_once_fixed(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "good", {"stories": {}})
    bad = tmp_path / "bad.manifest.json"
    bad.write_text("{not json")
    loads = _CountingLoads(monkeypatch)

    assert set(cross_plan.load_manifests(tmp_path)) == {"good"}

    # Not cached: the next read retries the parse instead of returning the
    # failure from cache.
    loads_before = loads.calls
    assert cross_plan.read_manifest_cached(bad) is None
    assert cross_plan.read_manifest_cached(bad) is None
    assert loads.calls == loads_before + 2

    bad.write_text(json.dumps({"stories": {"S9": {"status": "done"}}}))
    _bump_mtime(bad)

    assert cross_plan.read_manifest_cached(bad) == {"stories": {"S9": {"status": "done"}}}
    assert set(cross_plan.load_manifests(tmp_path)) == {"good", "bad"}


def test_a_non_object_manifest_is_skipped_not_cached(tmp_path, monkeypatch):
    path = tmp_path / "list.manifest.json"
    path.write_text(json.dumps([1, 2, 3]))
    loads = _CountingLoads(monkeypatch)

    assert cross_plan.load_manifests(tmp_path) == {}
    assert cross_plan.read_manifest_cached(path) is None
    assert loads.calls == 2


def test_a_malformed_manifest_warns_once_per_stamp_not_every_call(tmp_path, monkeypatch, caplog):
    _write_manifest(tmp_path, "good", {"stories": {}})
    bad = tmp_path / "bad.manifest.json"
    bad.write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        for _ in range(3):
            cross_plan.load_manifests(tmp_path)

    skips = [r for r in caplog.records if "unreadable manifest" in r.getMessage()]
    assert len(skips) == 1
    # The warning names the plan, never the filesystem path or exception text.
    assert skips[0].plan == "bad"
    assert "not json" not in skips[0].getMessage()
    assert str(bad) not in skips[0].getMessage()


def test_load_manifests_returns_only_files_that_still_exist(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "alpha", {"stories": {}})
    _write_manifest(tmp_path, "beta", {"stories": {}})
    _CountingLoads(monkeypatch)
    assert set(cross_plan.load_manifests(tmp_path)) == {"alpha", "beta"}

    (tmp_path / "beta.manifest.json").unlink()

    assert set(cross_plan.load_manifests(tmp_path)) == {"alpha"}


def test_exclude_plan_still_excludes(tmp_path, monkeypatch):
    _write_manifest(tmp_path, "alpha", {"stories": {}})
    _write_manifest(tmp_path, "beta", {"stories": {}})
    _CountingLoads(monkeypatch)

    assert set(cross_plan.load_manifests(tmp_path, exclude_plan="alpha")) == {"beta"}


def test_scan_all_plans_reads_through_the_cache(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"status": "tests_passed"}))
    _write_manifest(
        plan_dir,
        "alpha",
        {"stories": {"S1": {"status": "in_progress", "worktree": str(worktree)}}},
    )
    monkeypatch.setattr(paths, "PLAN_DIR", plan_dir)

    seen: list[Path] = []
    real = cross_plan.read_manifest_cached

    def _spy(path):
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(cross_plan, "read_manifest_cached", _spy)

    class _Bus:
        def __init__(self):
            self.published = []

        def publish(self, event):
            self.published.append(event)

    bus = _Bus()
    events = watchers.scan_all_plans(bus)

    assert [path.name for path in seen] == ["alpha.manifest.json"]
    assert [event["type"] for event in events] == ["agent_done"]
    assert [event["type"] for event in bus.published] == ["agent_done"]
