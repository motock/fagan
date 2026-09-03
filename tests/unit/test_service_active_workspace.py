"""Tests for active-workspace accessors on PipelineService (pipeline/service.py).

Story: add two thin delegators next to ``resolve_workspace`` /
``list_workspaces`` (service.py lines 354-416):

- ``PipelineService.get_active_workspace(self) -> str | None`` returns
  ``_store.get_active_workspace()`` — the module-level ``_store`` free
  variable, the same seam ``resolve_workspace`` uses at line 373.
- ``PipelineService.set_active_workspace(self, path: str | None) -> None``
  delegates to ``_store.set_active_workspace(path)``.

The persistence layer (``pipeline/store.py``) already landed in story 1 and is
covered by ``tests/unit/test_store_active_workspace.py``; these tests cover the
service-level delegation seam only. The shared ``plan_dir`` fixture from
``tests/unit/conftest.py`` patches ``pipeline.server.PLAN_DIR`` to a tmp
directory, and ``pipeline.server._store`` (a ``FileStore``) resolves PLAN_DIR
through that same binding at call time, so the round trips below exercise the
real on-disk behavior without touching the operator's ``~/.claude/plans/``.
"""

import hashlib
import inspect

import pytest

import pipeline.server as server_mod
from pipeline.service import PipelineService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    """A bare PipelineService (the brief's construction pattern)."""
    return PipelineService()


def _normalized_source(fn) -> str:
    """Source of ``fn`` with per-line trailing whitespace stripped.

    Trailing-whitespace-insensitive so cosmetic editor churn cannot break the
    do-not-modify guards, while any real edit to the guarded bodies does.
    """
    return "\n".join(
        line.rstrip() for line in inspect.getsource(fn).splitlines()
    )


def _digest(fn) -> str:
    return hashlib.sha256(_normalized_source(fn).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Existence, signatures, and delegation wiring (RED until the methods exist)
# ---------------------------------------------------------------------------


def test_get_active_workspace_method_exists():
    assert hasattr(PipelineService, "get_active_workspace")


def test_set_active_workspace_method_exists():
    assert hasattr(PipelineService, "set_active_workspace")


def test_get_active_workspace_signature():
    sig = inspect.signature(PipelineService.get_active_workspace)
    assert list(sig.parameters) == ["self"]
    # Return annotation must be Optional[str]: exactly ``str | None``.
    assert sig.return_annotation == (str | None)


def test_set_active_workspace_signature():
    sig = inspect.signature(PipelineService.set_active_workspace)
    assert list(sig.parameters) == ["self", "path"]
    assert sig.parameters["path"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert sig.parameters["path"].default is inspect.Parameter.empty
    assert sig.return_annotation is None


def test_get_active_workspace_delegates_to_store_free_var(service, monkeypatch):
    """get_active_workspace must read the module-level ``_store`` free var."""
    sentinel = "/abs/from-fake-store"
    calls = []

    class _FakeStore:
        def get_active_workspace(self):
            calls.append("get")
            return sentinel

    monkeypatch.setattr(server_mod, "_store", _FakeStore())
    assert service.get_active_workspace() == sentinel
    assert calls == ["get"]


def test_set_active_workspace_delegates_to_store_free_var(service, monkeypatch):
    """set_active_workspace must delegate to ``_store.set_active_workspace``."""
    calls = []

    class _FakeStore:
        def set_active_workspace(self, path):
            calls.append(path)

    monkeypatch.setattr(server_mod, "_store", _FakeStore())
    assert service.set_active_workspace("/abs/ws-a") is None
    assert calls == ["/abs/ws-a"]

    service.set_active_workspace(None)
    assert calls == ["/abs/ws-a", None]


def test_set_active_workspace_propagates_store_errors(service, monkeypatch):
    """Thin delegator: a store failure surfaces as-is (no swallowing)."""

    class _ExplodingStore:
        def set_active_workspace(self, path):
            raise RuntimeError("store exploded")

    monkeypatch.setattr(server_mod, "_store", _ExplodingStore())
    with pytest.raises(RuntimeError, match="store exploded"):
        service.set_active_workspace("/abs/ws-a")


# ---------------------------------------------------------------------------
# Happy path / boundary behavior against the real FileStore-backed _store
# ---------------------------------------------------------------------------


def test_fresh_plan_dir_returns_none(service, plan_dir):
    assert service.get_active_workspace() is None


def test_set_then_get_round_trips(service, plan_dir):
    service.set_active_workspace("/tmp/demo-repo")
    assert service.get_active_workspace() == "/tmp/demo-repo"


def test_set_none_after_set_clears_selection(service, plan_dir):
    service.set_active_workspace("/tmp/demo-repo")
    assert service.get_active_workspace() == "/tmp/demo-repo"
    service.set_active_workspace(None)
    assert service.get_active_workspace() is None


def test_set_none_on_fresh_dir_stays_none(service, plan_dir):
    service.set_active_workspace(None)
    assert service.get_active_workspace() is None


def test_set_overwrites_previous_value(service, plan_dir):
    """Boundary: set then overwrite replaces the value (no history/list)."""
    service.set_active_workspace("/abs/ws-first")
    service.set_active_workspace("/abs/ws-second")
    assert service.get_active_workspace() == "/abs/ws-second"
    # And clearing after an overwrite still clears.
    service.set_active_workspace(None)
    assert service.get_active_workspace() is None


def test_set_empty_string_clears_selection(service, plan_dir):
    service.set_active_workspace("/abs/ws-a")
    service.set_active_workspace("")
    assert service.get_active_workspace() is None


def test_service_sees_pre_seeded_store_state(service, plan_dir):
    """get reads whatever _store holds, including state set before the call."""
    (plan_dir / "active_workspace.json").write_text('{"path": "/abs/pre-seeded"}')
    assert service.get_active_workspace() == "/abs/pre-seeded"


def test_service_round_trip_lands_in_plan_dir(service, plan_dir):
    """The delegation must hit the FileStore whose PLAN_DIR is plan_dir."""
    service.set_active_workspace("/abs/ws-on-disk")
    on_disk = (plan_dir / "active_workspace.json").read_text()
    assert '"path"' in on_disk
    assert "/abs/ws-on-disk" in on_disk


def test_set_active_workspace_returns_none(service, plan_dir):
    assert service.set_active_workspace("/abs/ws-a") is None
    assert service.set_active_workspace(None) is None


def test_service_delegation_does_not_disturb_recent_workspaces(service, plan_dir):
    """The new accessors ride the same _store seam without side effects on
    the recents list that resolve_workspace/list_workspaces maintain."""
    service.set_active_workspace("/abs/ws-a")
    assert service.get_active_workspace() == "/abs/ws-a"
    assert server_mod._store.get_recent_workspaces() == []


# ---------------------------------------------------------------------------
# Placement + do-not-modify guards (mechanically checkable requirements)
# ---------------------------------------------------------------------------


def test_new_methods_placed_next_to_resolve_and_list_workspaces():
    """The two methods must live in the resolve_workspace/list_workspaces
    neighborhood (brief: service.py lines 354-416), i.e. between
    ``resolve_workspace`` and the next method after ``list_workspaces``
    (``approve_merge``). Order-relative, so inserting the pair does not
    shift the anchors."""
    resolve_line = PipelineService.resolve_workspace.__code__.co_firstlineno
    list_line = PipelineService.list_workspaces.__code__.co_firstlineno
    approve_line = PipelineService.approve_merge.__code__.co_firstlineno
    get_line = PipelineService.get_active_workspace.__code__.co_firstlineno
    set_line = PipelineService.set_active_workspace.__code__.co_firstlineno
    assert resolve_line < get_line < approve_line
    assert resolve_line < set_line < approve_line
    # Both inside the same neighborhood, and list_workspaces still sits
    # between resolve_workspace and approve_merge (untouched ordering).
    assert resolve_line < list_line < approve_line


def test_resolve_workspace_unchanged():
    """Do NOT modify resolve_workspace (brief). Digest of its normalized
    source at story start."""
    assert (
        _digest(PipelineService.resolve_workspace)
        == "add1799d7b9761a4d65f52692224ec239491d4ccf27329735532cb976cab329a"
    )


def test_list_workspaces_unchanged():
    """Do NOT modify list_workspaces (brief)."""
    assert (
        _digest(PipelineService.list_workspaces)
        == "67d6ab3cc76d33bedd7de0fc6cd53471cf3260d87efd1d5c7bbbdab6c22aa16f"
    )


def test_save_plan_unchanged():
    """Do NOT modify save_plan (brief)."""
    assert (
        _digest(PipelineService.save_plan)
        == "32b44890c7aa3e7545455b691b354a2abaf2332ff59af24595d7f61df7d607c8"
    )


def test_save_plan_still_uses_store_seam_unchanged():
    """save_plan's _store usage must be untouched by this story."""
    src = _normalized_source(PipelineService.save_plan)
    assert "_atomic_write_json(path, plan)" in src
    assert "active_workspace" not in src


def test_resolve_workspace_still_records_recents_via_store():
    """resolve_workspace's line-373 seam (_store.add_recent_workspace) must
    survive this story verbatim."""
    src = _normalized_source(PipelineService.resolve_workspace)
    assert "_store.add_recent_workspace(resolved_path)" in src


def test_list_workspaces_still_reads_store_recents():
    src = _normalized_source(PipelineService.list_workspaces)
    assert "_store.get_recent_workspaces()" in src


def test_store_module_untouched_by_this_story():
    """pipeline/store.py already landed in story 1; this story must not edit
    it. Both active-workspace methods must still exist on FileStore, with
    their story-1 bodies unchanged (digest of normalized source)."""
    from pipeline.server import FileStore

    assert hasattr(FileStore, "get_active_workspace")
    assert hasattr(FileStore, "set_active_workspace")
    assert (
        _digest(FileStore.get_active_workspace)
        == "16fff6ba868a18448929aa4583591c5cf5cebf45960ad488b1944038c616caf8"
    )
    assert (
        _digest(FileStore.set_active_workspace)
        == "c7a8842b3e5a1f7fd378f876e1f4f50c740f056bc9fe19c775bf42d7ce34b06b"
    )