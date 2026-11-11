"""Tests for the HostState seam (ADR 0012, decision 4).

``pipeline/host_state.py`` owns the host-local workspace records that used to
live on the store: the recent-workspaces list and the active workspace.  They
are host-local (not namespaced, never replicated), so they move behind a
``HostState`` protocol with a file-backed implementation, and
``build_host_state()`` is the only constructor.

What is graded here is behaviour through the public API only:

* the records round-trip through ``<root>/recent_workspaces.json`` and
  ``<root>/active_workspace.json`` (file names unchanged),
* recents are most-recent-first, de-duplicated and capped at 20,
* a malformed / non-list / non-string-bearing file degrades to ``[]`` (and to
  ``None`` for the active workspace) instead of raising,
* a failed write leaves no temporary file behind,
* the root is resolved at call time through the ``pipeline.server`` binding,
  so ``monkeypatch.setattr(pipeline.server, "PLAN_DIR", d)`` keeps landing,
* the four methods are gone from the store (protocol, ``FileStore`` and
  ``SqliteStore``) and the service reaches them through the host state
  instead: the service is driven with a store proxy that REFUSES the four
  workspace methods, so any leftover store call fails the test.
"""

import contextlib
import inspect
import json
import os
import subprocess
from typing import Protocol

import pytest

import pipeline.server as server_mod
from pipeline.service import PipelineService
from pipeline.store import FileStore, Store
from pipeline.store_sqlite import SqliteStore

try:  # the module under test does not exist yet -> RED, not a collection error
    from pipeline import host_state
except ImportError:  # pragma: no cover - exercised only while the seam is missing
    host_state = None

RECENT_FILE = "recent_workspaces.json"
ACTIVE_FILE = "active_workspace.json"
WORKSPACE_METHODS = (
    "get_recent_workspaces",
    "add_recent_workspace",
    "get_active_workspace",
    "set_active_workspace",
)


def _hs():
    """Build the host state through the only public constructor."""
    assert host_state is not None, "pipeline/host_state.py does not exist yet"
    return host_state.build_host_state()


@pytest.fixture
def refusing_store(monkeypatch):
    """Replace the server's store with a proxy that refuses the four methods.

    The service must reach the workspace records through the host state, so a
    leftover ``_store.get_active_workspace()`` (or any sibling) fails loudly.
    """

    class _StoreProxy:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            if name in WORKSPACE_METHODS:
                raise AssertionError(
                    f"the service must not use the store for {name}"
                )
            return getattr(self._real, name)

    proxy = _StoreProxy(server_mod._store)
    monkeypatch.setattr(server_mod, "_store", proxy)
    return proxy


def _init_git_repo(path):
    """Create a real, minimal git repo with one commit at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(path), capture_output=True, check=True,
    )
    return path


# ---------------------------------------------------------------------------
# The seam itself: protocol, implementation, single public constructor
# ---------------------------------------------------------------------------


def test_module_exposes_protocol_implementation_and_factory():
    assert host_state is not None, "pipeline/host_state.py does not exist yet"
    assert hasattr(host_state, "HostState"), "HostState protocol is missing"
    assert hasattr(host_state, "FileHostState"), "FileHostState is missing"
    assert issubclass(host_state.HostState, Protocol), (
        "HostState must be a typing.Protocol"
    )
    for name in WORKSPACE_METHODS:
        assert hasattr(host_state.HostState, name), (
            f"HostState must declare {name}"
        )


def test_build_host_state_returns_the_file_implementation():
    assert host_state is not None, "pipeline/host_state.py does not exist yet"
    built = host_state.build_host_state()
    assert isinstance(built, host_state.FileHostState)
    for name in WORKSPACE_METHODS:
        assert callable(getattr(built, name, None)), f"{name} is missing"


def test_workspace_methods_are_not_namespaced():
    """Host-local state is not namespaced data: no namespace argument."""
    built = _hs()
    expected_args = {
        "get_recent_workspaces": 0,
        "add_recent_workspace": 1,
        "get_active_workspace": 0,
        "set_active_workspace": 1,
    }
    for name, count in expected_args.items():
        params = inspect.signature(getattr(built, name)).parameters
        assert "namespace" not in params, f"{name} must not be namespaced"
        assert len(params) == count, f"{name} takes {len(params)} arguments"


# ---------------------------------------------------------------------------
# Recents: add/get, most-recent-first, de-duplicated, capped at 20
# ---------------------------------------------------------------------------


def test_added_workspace_is_read_back(plan_dir):
    hs = _hs()
    hs.add_recent_workspace("/abs/ws-a")
    assert hs.get_recent_workspaces() == ["/abs/ws-a"]


def test_readding_a_workspace_moves_it_to_the_front(plan_dir):
    hs = _hs()
    hs.add_recent_workspace("/abs/ws-a")
    hs.add_recent_workspace("/abs/ws-b")
    hs.add_recent_workspace("/abs/ws-a")
    assert hs.get_recent_workspaces() == ["/abs/ws-a", "/abs/ws-b"]


def test_recents_are_capped_at_twenty_and_evict_the_oldest(plan_dir):
    hs = _hs()
    for i in range(21):
        hs.add_recent_workspace(f"/abs/ws-{i:02d}")
    recents = hs.get_recent_workspaces()
    assert len(recents) == 20
    assert recents[0] == "/abs/ws-20", "the newest entry must come first"
    assert "/abs/ws-00" not in recents, "the oldest entry must be evicted"
    assert "/abs/ws-01" in recents


def test_malformed_recents_file_reads_as_empty(plan_dir):
    (plan_dir / RECENT_FILE).write_text("{not json")
    assert _hs().get_recent_workspaces() == []


def test_non_list_recents_file_reads_as_empty(plan_dir):
    (plan_dir / RECENT_FILE).write_text(json.dumps({"path": "/abs/ws-a"}))
    assert _hs().get_recent_workspaces() == []


def test_non_string_recents_entries_are_dropped(plan_dir):
    (plan_dir / RECENT_FILE).write_text(
        json.dumps(["/abs/ws-a", 5, None, ["/abs/ws-b"], {"path": "/abs/ws-c"}])
    )
    assert _hs().get_recent_workspaces() == ["/abs/ws-a"]


# ---------------------------------------------------------------------------
# Active workspace: round-trip, clearing, malformed input
# ---------------------------------------------------------------------------


def test_active_workspace_round_trips_a_path(plan_dir):
    hs = _hs()
    hs.set_active_workspace("/abs/ws-a")
    assert hs.get_active_workspace() == "/abs/ws-a"
    on_disk = json.loads((plan_dir / ACTIVE_FILE).read_text())
    assert on_disk["path"] == "/abs/ws-a"


def test_active_workspace_none_round_trips_and_clears(plan_dir):
    hs = _hs()
    hs.set_active_workspace("/abs/ws-a")
    hs.set_active_workspace(None)
    assert hs.get_active_workspace() is None


def test_malformed_active_workspace_file_reads_as_none(plan_dir):
    (plan_dir / ACTIVE_FILE).write_text("{not json")
    assert _hs().get_active_workspace() is None


def test_non_dict_active_workspace_file_reads_as_none(plan_dir):
    (plan_dir / ACTIVE_FILE).write_text(json.dumps(["/abs/ws-a"]))
    assert _hs().get_active_workspace() is None


# ---------------------------------------------------------------------------
# Durability: the records live directly under the root, writes are atomic
# ---------------------------------------------------------------------------


def test_records_live_directly_under_the_root(plan_dir):
    hs = _hs()
    hs.add_recent_workspace("/abs/ws-a")
    hs.set_active_workspace("/abs/ws-a")
    assert sorted(p.name for p in plan_dir.iterdir()) == [ACTIVE_FILE, RECENT_FILE]


def _break_atomic_replace(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)


def test_failed_recents_write_leaves_no_temporary_file(plan_dir, monkeypatch):
    (plan_dir / RECENT_FILE).write_text(json.dumps(["/abs/old"]))
    before = sorted(p.name for p in plan_dir.iterdir())
    hs = _hs()
    _break_atomic_replace(monkeypatch)
    with contextlib.suppress(OSError):
        hs.add_recent_workspace("/abs/new")
    assert sorted(p.name for p in plan_dir.iterdir()) == before
    assert json.loads((plan_dir / RECENT_FILE).read_text()) == ["/abs/old"]


def test_failed_active_write_leaves_no_temporary_file(plan_dir, monkeypatch):
    (plan_dir / ACTIVE_FILE).write_text(json.dumps({"path": "/abs/old"}))
    before = sorted(p.name for p in plan_dir.iterdir())
    hs = _hs()
    _break_atomic_replace(monkeypatch)
    with contextlib.suppress(OSError):
        hs.set_active_workspace("/abs/new")
    assert sorted(p.name for p in plan_dir.iterdir()) == before
    assert json.loads((plan_dir / ACTIVE_FILE).read_text())["path"] == "/abs/old"


# ---------------------------------------------------------------------------
# The root binding: resolved at call time through pipeline.server
# ---------------------------------------------------------------------------


def test_writes_land_in_the_patched_server_plan_dir(plan_dir, monkeypatch):
    hs = _hs()
    other = plan_dir.parent / "other-plans"
    other.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", other)
    hs.add_recent_workspace("/abs/ws-a")
    assert (other / RECENT_FILE).exists()
    assert not (plan_dir / RECENT_FILE).exists()


def test_root_is_resolved_at_call_time_not_at_construction(plan_dir, monkeypatch):
    """The binding is read per call, so a later re-patch still lands."""
    hs = _hs()
    other = plan_dir.parent / "other-plans"
    other.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", other)
    hs.add_recent_workspace("/abs/ws-a")
    assert (other / RECENT_FILE).exists()


# ---------------------------------------------------------------------------
# The four methods are gone from the store
# ---------------------------------------------------------------------------


def test_store_protocol_no_longer_declares_the_workspace_methods():
    for name in WORKSPACE_METHODS:
        assert not hasattr(Store, name), f"Store still declares {name}"


def test_file_store_no_longer_implements_the_workspace_methods():
    for name in WORKSPACE_METHODS:
        assert not hasattr(FileStore, name), f"FileStore still implements {name}"


def test_sqlite_store_no_longer_implements_the_workspace_methods():
    for name in WORKSPACE_METHODS:
        assert not hasattr(SqliteStore, name), f"SqliteStore still implements {name}"


# ---------------------------------------------------------------------------
# The service reaches the workspace records through the host state
# ---------------------------------------------------------------------------


def test_service_resolve_workspace_records_the_recent(
    plan_dir, refusing_store, tmp_path
):
    repo = _init_git_repo(tmp_path / "repo")
    result = PipelineService().resolve_workspace(str(repo))
    assert result.get("ok") is True
    recents = json.loads((plan_dir / RECENT_FILE).read_text())
    assert str(repo) in recents


def test_service_list_workspaces_reads_the_recents(
    plan_dir, refusing_store, tmp_path
):
    repo = _init_git_repo(tmp_path / "repo")
    (plan_dir / RECENT_FILE).write_text(json.dumps([str(repo)]))
    paths = {entry["path"] for entry in PipelineService().list_workspaces()}
    assert str(repo) in paths


def test_service_get_active_workspace_reads_the_active_record(
    plan_dir, refusing_store
):
    (plan_dir / ACTIVE_FILE).write_text(json.dumps({"path": "/abs/pre-seeded"}))
    assert PipelineService().get_active_workspace() == "/abs/pre-seeded"


def test_service_set_active_workspace_writes_the_active_record(
    plan_dir, refusing_store
):
    PipelineService().set_active_workspace("/abs/ws-a")
    on_disk = json.loads((plan_dir / ACTIVE_FILE).read_text())
    assert on_disk["path"] == "/abs/ws-a"
