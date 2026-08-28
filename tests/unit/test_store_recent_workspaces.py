"""Failing tests for recents persistence on FileStore (pipeline/store.py).

FileStore is expected to grow two methods:

- ``get_recent_workspaces() -> list[str]`` reads a JSON list of absolute
  path strings from ``recent_workspaces.json`` alongside the existing plan
  storage (PLAN_DIR). It must never raise: absent, empty, or malformed
  content all resolve to ``[]``.
- ``add_recent_workspace(path: str) -> None`` prepends ``path``,
  de-duplicates preserving most-recent-first order, caps the list at 20
  entries, and writes atomically (write-to-temp + os.replace, mirroring
  ``save_manifest``).

These tests are written to be RED until both methods exist on FileStore.
The shared ``plan_dir`` fixture from tests/unit/conftest.py patches
``pipeline.server.PLAN_DIR`` (and friends) to a tmp directory, and FileStore
resolves PLAN_DIR through that same binding at call time.
"""

import json

import pytest

from pipeline.server import FileStore

RECENTS_FILENAME = "recent_workspaces.json"


def _recents_path(plan_dir):
    return plan_dir / RECENTS_FILENAME


# ---------------------------------------------------------------------------
# get_recent_workspaces: absent / empty / malformed -> [] (never raises)
# ---------------------------------------------------------------------------

def test_get_recent_workspaces_returns_empty_list_when_file_missing(plan_dir):
    store = FileStore()
    assert store.get_recent_workspaces() == []


def test_get_recent_workspaces_returns_empty_list_when_file_empty(plan_dir):
    _recents_path(plan_dir).write_text("")
    store = FileStore()
    assert store.get_recent_workspaces() == []


def test_get_recent_workspaces_returns_empty_list_on_malformed_json(plan_dir):
    _recents_path(plan_dir).write_text("{not valid json")
    store = FileStore()
    assert store.get_recent_workspaces() == []


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"a": 1}),
        json.dumps("just a string"),
        json.dumps(5),
        json.dumps(None),
        json.dumps(True),
    ],
    ids=["dict", "string", "int", "null", "bool"],
)
def test_get_recent_workspaces_returns_empty_list_for_non_list_json(plan_dir, raw):
    _recents_path(plan_dir).write_text(raw)
    store = FileStore()
    assert store.get_recent_workspaces() == []


# ---------------------------------------------------------------------------
# add_recent_workspace + get_recent_workspaces: round trip, dedup, ordering, cap
# ---------------------------------------------------------------------------

def test_add_then_get_round_trips_single_entry(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/path/workspace-a")
    assert store.get_recent_workspaces() == ["/abs/path/workspace-a"]


def test_add_recent_workspace_order_is_most_recent_first(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/ws1")
    store.add_recent_workspace("/abs/ws2")
    store.add_recent_workspace("/abs/ws3")
    assert store.get_recent_workspaces() == ["/abs/ws3", "/abs/ws2", "/abs/ws1"]


def test_add_existing_path_moves_to_front_without_duplicating(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/ws-a")
    store.add_recent_workspace("/abs/ws-b")
    store.add_recent_workspace("/abs/ws-a")
    assert store.get_recent_workspaces() == ["/abs/ws-a", "/abs/ws-b"]


def test_add_existing_path_does_not_grow_list_length(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/ws-a")
    store.add_recent_workspace("/abs/ws-b")
    store.add_recent_workspace("/abs/ws-a")
    assert len(store.get_recent_workspaces()) == 2


def test_add_recent_workspace_at_exactly_20_entries_keeps_all(plan_dir):
    store = FileStore()
    paths = [f"/abs/ws{i}" for i in range(1, 21)]
    for path in paths:
        store.add_recent_workspace(path)
    assert store.get_recent_workspaces() == list(reversed(paths))


def test_add_recent_workspace_beyond_20_drops_oldest(plan_dir):
    store = FileStore()
    paths = [f"/abs/ws{i}" for i in range(1, 22)]
    for path in paths:
        store.add_recent_workspace(path)
    result = store.get_recent_workspaces()
    assert len(result) == 20
    assert "/abs/ws1" not in result
    assert result == list(reversed(paths[1:]))


# ---------------------------------------------------------------------------
# Storage location: alongside PLAN_DIR, not a hardcoded/new-config path.
# ---------------------------------------------------------------------------

def test_add_recent_workspace_writes_to_plan_dir(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/ws-a")
    assert _recents_path(plan_dir).exists()
    on_disk = json.loads(_recents_path(plan_dir).read_text())
    assert on_disk == ["/abs/ws-a"]


def test_get_recent_workspaces_reads_from_plan_dir(plan_dir):
    _recents_path(plan_dir).write_text(json.dumps(["/abs/pre-seeded"]))
    store = FileStore()
    assert store.get_recent_workspaces() == ["/abs/pre-seeded"]


# ---------------------------------------------------------------------------
# Atomic write: write-to-temp then os.replace, mirroring save_manifest.
# ---------------------------------------------------------------------------

def test_add_recent_workspace_writes_atomically_via_os_replace(plan_dir, monkeypatch):
    from pipeline import store as store_mod

    calls = []
    real_replace = store_mod.os.replace

    def fake_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", fake_replace)

    store = FileStore()
    store.add_recent_workspace("/abs/ws-atomic")

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst.endswith(RECENTS_FILENAME)
    assert src != dst
