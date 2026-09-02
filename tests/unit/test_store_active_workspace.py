"""Tests for active-workspace persistence on FileStore (pipeline/store.py).

Covers FileStore active-workspace get/set: negative and boundary cases for
reading and writing ``PLAN_DIR / active_workspace.json``.

- ``get_active_workspace() -> str | None`` reads ``PLAN_DIR /
  active_workspace.json``, which holds a JSON object ``{"path": "<abs
  path>"}``. It returns the path string when the file exists, parses as a
  dict, and ``path`` is a non-empty str. It must NEVER raise: missing,
  empty, malformed, non-dict, or bad-``path`` content all resolve to
  ``None`` (same tolerance as ``get_recent_workspaces``).
- ``set_active_workspace(path: str | None) -> None`` writes
  ``{"path": path}`` atomically: write to
  ``PLAN_DIR / f'active_workspace.json.tmp.{os.getpid()}'`` then
  ``os.replace`` onto the target (mirroring ``add_recent_workspace``'s
  try/except BaseException cleanup). ``None`` or ``""`` clears the
  selection; a string replaces any previous selection.

The shared ``plan_dir`` fixture from tests/unit/conftest.py patches
``pipeline.server.PLAN_DIR`` to a tmp directory, and FileStore resolves
PLAN_DIR through that same binding at call time. No path validation
belongs at this layer — that happens in pipeline/workspace.py — so
arbitrary strings must round-trip untouched.
"""

import contextlib
import json
import os

import pytest

from pipeline.server import FileStore, Store

ACTIVE_FILENAME = "active_workspace.json"


def _active_path(plan_dir):
    return plan_dir / ACTIVE_FILENAME


# ---------------------------------------------------------------------------
# get_active_workspace: absent / empty / malformed -> None (never raises)
# ---------------------------------------------------------------------------

def test_get_active_workspace_returns_none_when_file_missing(plan_dir):
    store = FileStore()
    assert store.get_active_workspace() is None


def test_get_active_workspace_returns_none_when_file_empty(plan_dir):
    _active_path(plan_dir).write_text("")
    store = FileStore()
    assert store.get_active_workspace() is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not valid json",
        "   ",
        "\n",
    ],
    ids=["garbage", "whitespace-only", "newline-only"],
)
def test_get_active_workspace_returns_none_on_malformed_json(plan_dir, raw):
    _active_path(plan_dir).write_text(raw)
    store = FileStore()
    assert store.get_active_workspace() is None


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(["/abs/ws-a"]),
        json.dumps("just a string"),
        json.dumps(5),
        json.dumps(None),
        json.dumps(True),
    ],
    ids=["list", "string", "int", "null", "bool"],
)
def test_get_active_workspace_returns_none_for_non_dict_json(plan_dir, raw):
    _active_path(plan_dir).write_text(raw)
    store = FileStore()
    assert store.get_active_workspace() is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"path": 5},
        {"path": ""},
        {"path": None},
        {"path": True},
        {"path": ["/abs/ws-a"]},
        {"path": {"nested": "/abs/ws-a"}},
        {"other": "/abs/ws-a"},
    ],
    ids=[
        "path-missing",
        "path-int",
        "path-empty-str",
        "path-null",
        "path-bool",
        "path-list",
        "path-dict",
        "path-key-absent",
    ],
)
def test_get_active_workspace_returns_none_for_unusable_path_value(plan_dir, payload):
    _active_path(plan_dir).write_text(json.dumps(payload))
    store = FileStore()
    assert store.get_active_workspace() is None


def test_get_active_workspace_returns_path_from_valid_file(plan_dir):
    _active_path(plan_dir).write_text(json.dumps({"path": "/abs/pre-seeded"}))
    store = FileStore()
    result = store.get_active_workspace()
    assert result == "/abs/pre-seeded"
    assert isinstance(result, str)


def test_get_active_workspace_is_stable_across_repeated_reads(plan_dir):
    _active_path(plan_dir).write_text(json.dumps({"path": "/abs/ws-a"}))
    store = FileStore()
    assert store.get_active_workspace() == "/abs/ws-a"
    assert store.get_active_workspace() == "/abs/ws-a"


# ---------------------------------------------------------------------------
# set_active_workspace + get_active_workspace: round trip, replace, clear
# ---------------------------------------------------------------------------

def test_set_then_get_round_trips_single_path(plan_dir):
    store = FileStore()
    store.set_active_workspace("/tmp/demo-repo")
    assert store.get_active_workspace() == "/tmp/demo-repo"


def test_set_active_workspace_writes_object_with_path_key_to_plan_dir(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-a")
    assert _active_path(plan_dir).exists()
    on_disk = json.loads(_active_path(plan_dir).read_text())
    assert on_disk == {"path": "/abs/ws-a"}


def test_get_active_workspace_reads_from_plan_dir(plan_dir):
    _active_path(plan_dir).write_text(json.dumps({"path": "/abs/pre-seeded"}))
    store = FileStore()
    assert store.get_active_workspace() == "/abs/pre-seeded"


def test_second_set_replaces_first_selection(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-first")
    store.set_active_workspace("/abs/ws-second")
    assert store.get_active_workspace() == "/abs/ws-second"
    on_disk = json.loads(_active_path(plan_dir).read_text())
    assert on_disk == {"path": "/abs/ws-second"}


def test_second_set_does_not_grow_file_into_a_list_or_history(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-first")
    store.set_active_workspace("/abs/ws-second")
    on_disk = json.loads(_active_path(plan_dir).read_text())
    assert isinstance(on_disk, dict)
    assert list(on_disk.keys()) == ["path"]


def test_set_none_after_set_clears_selection(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-a")
    store.set_active_workspace(None)
    assert store.get_active_workspace() is None


def test_set_empty_string_after_set_clears_selection(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-a")
    store.set_active_workspace("")
    assert store.get_active_workspace() is None


def test_set_none_on_fresh_store_is_safe_and_stays_none(plan_dir):
    store = FileStore()
    store.set_active_workspace(None)
    assert store.get_active_workspace() is None


def test_set_empty_string_on_fresh_store_is_safe_and_stays_none(plan_dir):
    store = FileStore()
    store.set_active_workspace("")
    assert store.get_active_workspace() is None


def test_set_active_workspace_returns_none(plan_dir):
    store = FileStore()
    assert store.set_active_workspace("/abs/ws-a") is None
    assert store.set_active_workspace(None) is None


def test_set_active_workspace_accepts_any_string_without_validation(plan_dir):
    # Pure persistence layer: no absoluteness/existence checks here —
    # validation lives in pipeline/workspace.py at the service/route layer.
    store = FileStore()
    store.set_active_workspace("relative/not-absolute")
    assert store.get_active_workspace() == "relative/not-absolute"
    store.set_active_workspace("/abs/does-not-exist-anywhere")
    assert store.get_active_workspace() == "/abs/does-not-exist-anywhere"


# ---------------------------------------------------------------------------
# Storage location: alongside PLAN_DIR, not a hardcoded/new-config path.
# ---------------------------------------------------------------------------

def test_active_workspace_file_is_named_active_workspace_json(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-a")
    assert _active_path(plan_dir).exists()
    assert _active_path(plan_dir).name == "active_workspace.json"


# ---------------------------------------------------------------------------
# Atomic write: write-to-temp then os.replace, mirroring add_recent_workspace.
# ---------------------------------------------------------------------------

def test_set_active_workspace_writes_atomically_via_os_replace(plan_dir, monkeypatch):
    from pipeline import store as store_mod

    calls = []
    real_replace = store_mod.os.replace

    def fake_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", fake_replace)

    store = FileStore()
    store.set_active_workspace("/abs/ws-atomic")

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst.endswith(ACTIVE_FILENAME)
    assert src.endswith(f"active_workspace.json.tmp.{os.getpid()}")
    assert src != dst


def test_set_active_workspace_leaves_no_tmp_files_behind(plan_dir):
    store = FileStore()
    store.set_active_workspace("/abs/ws-a")
    store.set_active_workspace("/abs/ws-b")
    leftovers = list(plan_dir.glob("active_workspace.json.tmp.*"))
    assert leftovers == []


def test_failed_replace_cleans_up_tmp_file_and_preserves_previous_value(
    plan_dir, monkeypatch
):
    from pipeline import store as store_mod

    _active_path(plan_dir).write_text(json.dumps({"path": "/abs/old"}))

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store_mod.os, "replace", boom)

    store = FileStore()
    # The cleanup mirrors add_recent_workspace's try/except BaseException:
    # whether the implementation swallows or propagates the failure, the tmp
    # file must be removed and the previous selection must survive intact.
    with contextlib.suppress(OSError):
        store.set_active_workspace("/abs/new")

    assert list(plan_dir.glob("active_workspace.json.tmp.*")) == []
    assert store.get_active_workspace() == "/abs/old"


# ---------------------------------------------------------------------------
# Coexistence: the new methods must not disturb the existing recents methods.
# ---------------------------------------------------------------------------

def test_active_workspace_storage_does_not_disturb_recent_workspaces(plan_dir):
    store = FileStore()
    store.add_recent_workspace("/abs/ws-a")
    store.set_active_workspace("/abs/ws-a")

    assert store.get_recent_workspaces() == ["/abs/ws-a"]
    assert store.get_active_workspace() == "/abs/ws-a"

    recents_on_disk = json.loads((plan_dir / "recent_workspaces.json").read_text())
    assert recents_on_disk == ["/abs/ws-a"]


def test_store_protocol_declares_active_workspace_methods():
    """Store Protocol must declare every public FileStore method (precedent: be1f72a / #476)."""
    assert hasattr(Store, "get_active_workspace")
    assert hasattr(Store, "set_active_workspace")
