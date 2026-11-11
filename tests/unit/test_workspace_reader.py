# ruff: noqa: F811, I001
# The client fixture is imported from _dashboard_helpers and used as a test parameter, which F811 flags falsely (same pattern as tests/unit/test_dashboard_*.py, which pyproject.toml exempts).
"""Tests for the new ``pipeline/workspace_reader.py`` seam (ADR 0012, decision 12).

The reader owns the two workspace reads that used to live inside
``FileStore``:

  * ``read_story_log(plan_name, story_key, manifest, lines=200)`` ->
    ``{"available": bool, "lines": [str, ...]}``
  * ``read_worktree_file(story, filename)`` ->
    ``{"available": bool, "text": str}``

Every containment check is part of the contract and is exercised here:
the story log must resolve inside the plans directory, the worktree file
must resolve inside ``WORKTREE_ROOT``, and any unsafe input degrades to
the unavailable body instead of raising.

The route-level checks (ADR 0012 open question 3) live at the bottom:
each dashboard route that reads workspace content must answer HTTP 200
with the unavailable body - and no file content - for a traversal path
and for a worktree outside the root.
"""

from __future__ import annotations

import builtins
import pathlib

import pytest

from pipeline import server
from pipeline.store import FileStore
from tests.unit._dashboard_helpers import _write_manifest, client  # noqa: F401


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reader():
    """Return a fresh LocalWorkspaceReader (imported lazily so the module-level
    import error is reported per test, not at collection time)."""
    from pipeline.workspace_reader import LocalWorkspaceReader

    return LocalWorkspaceReader()


def _patch_reader_method(monkeypatch, method, sentinel):
    """Make every LocalWorkspaceReader.<method> return ``sentinel``."""
    from pipeline.workspace_reader import LocalWorkspaceReader

    monkeypatch.setattr(
        LocalWorkspaceReader, method, lambda self, *a, **k: sentinel, raising=False
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(log):
    return {"stories": {"S1": {"log": log}}}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plans(tmp_path, monkeypatch):
    """The plans directory the story log is read from."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """The worktree root every worktree file must resolve inside."""
    directory = tmp_path / "worktrees"
    directory.mkdir()
    monkeypatch.setattr(server, "WORKTREE_ROOT", directory)
    return directory


@pytest.fixture
def plan_root(tmp_path, monkeypatch):
    """The plans directory the dashboard routes read from."""
    from app import dashboard as d

    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def outside_worktree(tmp_path):
    """An absolute directory OUTSIDE any worktree root."""
    directory = tmp_path / "outside"
    directory.mkdir()
    return directory


@pytest.fixture
def store_tripwire(monkeypatch):
    """Record every workspace read that still goes through the store.

    The store no longer carries these reads (ADR 0012 decision 1), so the
    tripwire only arms itself if such a method ever comes back: a route
    that reaches the workspace through the store is recorded here.
    """
    calls: list[str] = []
    store_cls = type(server._store)
    for name in ("get_worktree_file", "get_story_log"):
        original = getattr(store_cls, name, None)
        if original is None:
            continue

        def _wrapper(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(store_cls, name, _wrapper, raising=False)
    return calls


# ---------------------------------------------------------------------------
# the reader surface
# ---------------------------------------------------------------------------


def test_workspace_reader_is_a_protocol_with_the_documented_surface():
    import typing

    from pipeline.workspace_reader import WorkspaceReader

    assert typing.Protocol in WorkspaceReader.__mro__
    assert callable(WorkspaceReader.read_story_log)
    assert callable(WorkspaceReader.read_worktree_file)


def test_local_workspace_reader_matches_the_protocol_signatures():
    import inspect

    from pipeline.workspace_reader import LocalWorkspaceReader, WorkspaceReader

    for name, expected in (
        ("read_story_log", ["plan_name", "story_key", "manifest", "lines"]),
        ("read_worktree_file", ["story", "filename"]),
    ):
        protocol_params = list(
            inspect.signature(getattr(WorkspaceReader, name)).parameters
        )
        impl_params = list(
            inspect.signature(getattr(LocalWorkspaceReader, name)).parameters
        )
        assert protocol_params == expected
        assert impl_params == expected


def test_local_workspace_reader_defaults_the_story_log_tail_to_200():
    import inspect

    from pipeline.workspace_reader import LocalWorkspaceReader

    signature = inspect.signature(LocalWorkspaceReader.read_story_log)
    assert signature.parameters["lines"].default == 200


# ---------------------------------------------------------------------------
# read_story_log
# ---------------------------------------------------------------------------


def test_read_story_log_returns_the_tail_of_the_log_file(plans, roots):
    _write(plans / "demo.S1.log", "\n".join(f"line {i}" for i in range(10)))

    got = _reader().read_story_log(
        "demo", "S1", _manifest("demo.S1.log"), lines=3
    )

    assert got == {"available": True, "lines": ["line 7", "line 8", "line 9"]}


def test_read_story_log_defaults_to_the_last_200_lines(plans, roots):
    _write(plans / "demo.S1.log", "\n".join(f"line {i}" for i in range(300)))

    got = _reader().read_story_log("demo", "S1", _manifest("demo.S1.log"))

    assert got["available"] is True
    assert len(got["lines"]) == 200
    assert got["lines"][-1] == "line 299"


@pytest.mark.parametrize(
    "lines,expected", [(0, 200), (1, 1), (501, 500), ("x", 200)]
)
def test_read_story_log_clamps_the_requested_tail_length(
    plans, roots, lines, expected
):
    _write(plans / "demo.S1.log", "\n".join(f"line {i}" for i in range(600)))

    got = _reader().read_story_log(
        "demo", "S1", _manifest("demo.S1.log"), lines=lines
    )

    assert got["available"] is True
    assert len(got["lines"]) == expected


def test_read_story_log_rejects_an_absolute_log_path(plans, roots, tmp_path):
    secret = _write(tmp_path / "secret.log", "SECRET\n")

    got = _reader().read_story_log("demo", "S1", _manifest(str(secret)))

    assert got == {"available": False, "lines": []}


def test_read_story_log_rejects_a_log_path_outside_the_plans_directory(
    plans, roots, tmp_path
):
    _write(tmp_path / "secret.log", "SECRET\n")

    got = _reader().read_story_log("demo", "S1", _manifest("../secret.log"))

    assert got == {"available": False, "lines": []}


def test_read_story_log_reports_a_missing_log_file_as_unavailable(plans, roots):
    got = _reader().read_story_log("demo", "S1", _manifest("demo.S1.log"))

    assert got == {"available": False, "lines": []}


def test_read_story_log_reports_an_unknown_story_as_unavailable(plans, roots):
    got = _reader().read_story_log("demo", "S9", _manifest("demo.S1.log"))

    assert got == {"available": False, "lines": []}


def test_read_story_log_swallows_os_errors(plans, roots, monkeypatch):
    _write(plans / "demo.S1.log", "line\n")

    def _boom(*args, **kwargs):
        raise OSError("read failed")

    monkeypatch.setattr(pathlib.Path, "open", _boom)
    monkeypatch.setattr(builtins, "open", _boom)

    got = _reader().read_story_log("demo", "S1", _manifest("demo.S1.log"))

    assert got == {"available": False, "lines": []}


# ---------------------------------------------------------------------------
# read_worktree_file
# ---------------------------------------------------------------------------


def test_read_worktree_file_reads_a_plain_file_inside_the_worktree(roots):
    worktree = roots / "S1"
    _write(worktree / "notes.md", "hello")

    got = _reader().read_worktree_file({"worktree": str(worktree)}, "notes.md")

    assert got == {"available": True, "text": "hello"}


@pytest.mark.parametrize(
    "filename", ["sub/notes.md", "sub\\notes.md", ".", "..", "", "../escape.md"]
)
def test_read_worktree_file_rejects_unsafe_filenames(roots, filename):
    worktree = roots / "S1"
    _write(worktree / "notes.md", "hello")

    got = _reader().read_worktree_file({"worktree": str(worktree)}, filename)

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_rejects_a_relative_worktree(roots):
    got = _reader().read_worktree_file({"worktree": "relative/dir"}, "notes.md")

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_rejects_a_story_that_is_not_a_mapping(roots):
    got = _reader().read_worktree_file("not-a-dict", "notes.md")

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_rejects_a_target_outside_the_worktree_root(
    roots, outside_worktree
):
    _write(outside_worktree / "notes.md", "SECRET")

    got = _reader().read_worktree_file(
        {"worktree": str(outside_worktree)}, "notes.md"
    )

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_rejects_a_symlink_pointing_outside_the_root(
    roots, tmp_path
):
    worktree = roots / "S1"
    secret = _write(tmp_path / "secret.md", "SECRET")
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "escape.md").symlink_to(secret)

    got = _reader().read_worktree_file({"worktree": str(worktree)}, "escape.md")

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_reports_a_missing_file_as_unavailable(roots):
    worktree = roots / "S1"
    worktree.mkdir(parents=True, exist_ok=True)

    got = _reader().read_worktree_file({"worktree": str(worktree)}, "absent.md")

    assert got == {"available": False, "text": ""}


def test_read_worktree_file_swallows_os_errors(roots, monkeypatch):
    worktree = roots / "S1"
    _write(worktree / "notes.md", "hello")

    def _boom(*args, **kwargs):
        raise OSError("read failed")

    monkeypatch.setattr(pathlib.Path, "open", _boom)
    monkeypatch.setattr(builtins, "open", _boom)

    got = _reader().read_worktree_file({"worktree": str(worktree)}, "notes.md")

    assert got == {"available": False, "text": ""}


# ---------------------------------------------------------------------------
# the store no longer reads the workspace; the service delegates to the reader
# ---------------------------------------------------------------------------


def test_file_store_no_longer_reads_the_workspace():
    """ADR 0012 decision 1: the store is not a workspace reader any more."""
    store = FileStore()

    for name in ("get_story_log", "get_worktree_file"):
        assert not hasattr(store, name), f"FileStore still exposes {name}"


def test_pipeline_service_delegates_story_log_reads_to_the_reader(monkeypatch):
    from pipeline.server import PipelineService

    sentinel = {"available": True, "lines": ["delegated"]}
    _patch_reader_method(monkeypatch, "read_story_log", sentinel)

    assert PipelineService().get_story_log("demo", "S1", {}) == sentinel


def test_pipeline_service_delegates_worktree_file_reads_to_the_reader(monkeypatch):
    from pipeline.server import PipelineService

    sentinel = {"available": True, "text": "delegated"}
    _patch_reader_method(monkeypatch, "read_worktree_file", sentinel)

    got = PipelineService().get_worktree_file({"worktree": "/tmp"}, "notes.md")

    assert got == sentinel


# ---------------------------------------------------------------------------
# ADR 0012 open question 3: one check per dashboard route
# ---------------------------------------------------------------------------


def test_log_route_hides_a_traversal_log_path(client, plan_root, tmp_path):
    _write(tmp_path / "secret.log", "SECRET-CONTENT\n")
    _write_manifest(plan_root, "demo", {"S1": {"log": "../secret.log"}})

    res = client.get("/api/plans/demo/stories/S1/log")

    assert res.status_code == 200
    assert res.json() == {"available": False, "lines": []}
    assert "SECRET-CONTENT" not in res.text


def test_log_route_hides_an_absolute_log_path(client, plan_root, tmp_path):
    secret = _write(tmp_path / "secret.log", "SECRET-CONTENT\n")
    _write_manifest(plan_root, "demo", {"S1": {"log": str(secret)}})

    res = client.get("/api/plans/demo/stories/S1/log")

    assert res.status_code == 200
    assert res.json() == {"available": False, "lines": []}
    assert "SECRET-CONTENT" not in res.text


def test_checklist_route_hides_a_worktree_outside_the_root(
    client, plan_root, outside_worktree
):
    _write(outside_worktree / ".agent_plan.md", "SECRET-CONTENT")
    _write(outside_worktree / ".agent_scratchpad.md", "SECRET-CONTENT")
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(outside_worktree)}},
    )

    res = client.get("/api/plans/demo/stories/S1/checklist")

    assert res.status_code == 200
    body = res.json()
    assert body["plan"] == {"available": False, "text": ""}
    assert body["scratchpad"] == {"available": False, "text": ""}
    assert body["progress"] is None
    assert "SECRET-CONTENT" not in res.text


def test_checklist_route_hides_a_traversal_worktree_path(client, plan_root, tmp_path):
    outside = tmp_path / "outside"
    _write(outside / ".agent_plan.md", "SECRET-CONTENT")
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(outside / ".." / "outside")}},
    )

    res = client.get("/api/plans/demo/stories/S1/checklist")

    assert res.status_code == 200
    assert res.json()["plan"] == {"available": False, "text": ""}
    assert "SECRET-CONTENT" not in res.text


def test_replay_route_hides_a_worktree_outside_the_root(
    client, plan_root, outside_worktree
):
    _write(outside_worktree / "agent.log", "SECRET-CONTENT\n")
    _write(outside_worktree / "review.log", "SECRET-CONTENT\n")
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(outside_worktree)}},
    )

    res = client.get("/api/plans/demo/stories/S1/replay")

    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["events"] == []
    assert body["sources"] == {
        "journal": False,
        "agent.log": False,
        "review.log": False,
    }
    assert "SECRET-CONTENT" not in res.text


def test_plan_route_hides_a_worktree_outside_the_root(
    client, plan_root, outside_worktree
):
    _write(outside_worktree / ".agent_plan.md", "SECRET-CONTENT")
    _write(outside_worktree / ".agent_scratchpad.md", "SECRET-CONTENT")
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(outside_worktree)}},
    )

    res = client.get("/api/plans/demo")

    assert res.status_code == 200
    assert "progress" not in res.json()["stories"]["S1"]
    assert "SECRET-CONTENT" not in res.text


def test_dashboard_routes_do_not_read_workspace_files_through_the_store(
    client, plan_root, outside_worktree, store_tripwire
):
    _write(outside_worktree / "agent.log", "line\n")
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(outside_worktree)}},
    )

    assert client.get("/api/plans/demo/stories/S1/checklist").status_code == 200
    assert client.get("/api/plans/demo/stories/S1/replay").status_code == 200
    assert client.get("/api/plans/demo/stories/S1/log").status_code == 200

    assert store_tripwire == []


def test_dashboard_routes_read_workspace_files_through_the_reader(
    client, plan_root, monkeypatch
):
    sentinel = {"available": True, "text": "READER-SENTINEL"}
    _patch_reader_method(monkeypatch, "read_worktree_file", sentinel)
    _write_manifest(
        plan_root,
        "demo",
        {"S1": {"status": "in_progress", "worktree": str(plan_root)}},
    )

    res = client.get("/api/plans/demo/stories/S1/checklist")

    assert res.status_code == 200
    assert res.json()["plan"] == sentinel
    assert res.json()["scratchpad"] == sentinel
