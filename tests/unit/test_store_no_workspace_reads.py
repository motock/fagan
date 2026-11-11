"""ADR 0012 decision 1: the Store is not a workspace reader.

The store's control plane used to expose two filesystem reads --
``get_story_log`` and ``get_worktree_file``. Both now live behind the
``WorkspaceReader`` seam (``pipeline/workspace_reader.py``), so no
control-plane method returns or reads a filesystem path.

These tests are the RED half of that migration:

* the ``Store`` protocol, ``FileStore`` and ``SqliteStore`` must NOT carry
  ``get_story_log`` / ``get_worktree_file`` any more,
* the ``WorkspaceReader`` must remain the only reader (it keeps both
  reads, and the store does not grow a replacement under another name),
* the service keeps its delegating accessors -- the removal is about the
  store surface, not about the control plane losing the feature.

The story-log and worktree-file behaviour that used to be asserted
against the store is asserted here against the reader instead, with the
same expectations.
"""

from __future__ import annotations

import pytest

from pipeline.store import FileStore, Store
from pipeline.store_sqlite import SqliteStore

REMOVED_READS = ("get_story_log", "get_worktree_file")


# ---------------------------------------------------------------------------
# the store surface no longer reads the workspace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REMOVED_READS)
def test_store_protocol_does_not_declare_the_workspace_reads(name):
    """The Store protocol must not declare a workspace read."""
    assert not hasattr(Store, name), f"Store still declares {name}"


@pytest.mark.parametrize("name", REMOVED_READS)
def test_file_store_does_not_expose_the_workspace_reads(name):
    """FileStore must not expose a workspace read."""
    assert not hasattr(FileStore, name), f"FileStore still exposes {name}"
    assert not hasattr(FileStore(), name), f"FileStore() still exposes {name}"


@pytest.mark.parametrize("name", REMOVED_READS)
def test_sqlite_store_does_not_expose_the_workspace_reads(name, tmp_path):
    """SqliteStore must not expose a workspace read either."""
    store = SqliteStore(str(tmp_path / "store.db"))
    try:
        assert not hasattr(SqliteStore, name), f"SqliteStore still exposes {name}"
        assert not hasattr(store, name), f"SqliteStore() still exposes {name}"
    finally:
        store.close()


@pytest.mark.parametrize("name", REMOVED_READS)
def test_no_store_implementation_reads_the_workspace(name):
    """No Store implementation may keep a workspace read under another name."""
    for implementation in (Store, FileStore, SqliteStore):
        public = [
            attribute
            for attribute in dir(implementation)
            if not attribute.startswith("_")
            and ("story_log" in attribute or "worktree_file" in attribute)
        ]
        assert public == [], f"{implementation.__name__} still exposes {public}"


def test_the_store_surface_keeps_its_other_reads():
    """Only the two workspace reads are removed; the rest of the surface stays."""
    for name in (
        "get_manifest",
        "get_manifest_or_none",
        "get_journal",
        "get_journal_final_ts",
        "get_decisions",
        "get_notifications",
    ):
        assert hasattr(Store, name), f"Store lost {name}"
        assert hasattr(FileStore, name), f"FileStore lost {name}"
        assert hasattr(SqliteStore, name), f"SqliteStore lost {name}"


# ---------------------------------------------------------------------------
# the workspace reader is the only reader
# ---------------------------------------------------------------------------


def test_the_workspace_reader_keeps_both_reads():
    """The reader seam still answers both workspace reads."""
    from pipeline.workspace_reader import LocalWorkspaceReader, WorkspaceReader

    assert callable(WorkspaceReader.read_story_log)
    assert callable(WorkspaceReader.read_worktree_file)
    assert callable(LocalWorkspaceReader.read_story_log)
    assert callable(LocalWorkspaceReader.read_worktree_file)


def test_the_service_still_answers_both_workspace_reads(monkeypatch, tmp_path):
    """PipelineService keeps its delegating accessors (the store is not the reader)."""
    from pipeline.server import PipelineService
    from pipeline.workspace_reader import LocalWorkspaceReader

    monkeypatch.setattr(
        LocalWorkspaceReader, "read_story_log", lambda *a, **k: {"available": True}
    )
    monkeypatch.setattr(
        LocalWorkspaceReader, "read_worktree_file", lambda *a, **k: {"available": True}
    )

    service = PipelineService()
    assert service.get_story_log("demo", "S1", {}) == {"available": True}
    assert service.get_worktree_file({"worktree": str(tmp_path)}, "notes.md") == {
        "available": True
    }


# ---------------------------------------------------------------------------
# the story-log read, now owned by the reader
# ---------------------------------------------------------------------------


@pytest.fixture
def plans(tmp_path, monkeypatch):
    """The plans directory the story log is read from."""
    from pipeline import server

    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """The worktree root every worktree file must resolve inside."""
    from pipeline import server

    directory = tmp_path / "worktrees"
    directory.mkdir()
    monkeypatch.setattr(server, "WORKTREE_ROOT", directory)
    return directory


def _reader():
    from pipeline.workspace_reader import LocalWorkspaceReader

    return LocalWorkspaceReader()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(log):
    return {"stories": {"S1": {"log": log}}}


def test_the_story_log_is_missing_and_present(plans, roots):
    """A story log is read relative to the plans directory, fail-open."""
    assert _reader().read_story_log("contract-plan", "S1", {}) == {
        "available": False,
        "lines": [],
    }

    (plans / "contract-plan.S1.log").write_text("a\nb\nc\n")

    got = _reader().read_story_log(
        "contract-plan",
        "S1",
        {"stories": {"S1": {"log": "contract-plan.S1.log"}}},
        lines=2,
    )

    assert got == {"available": True, "lines": ["b", "c"]}


def test_the_story_log_rejects_an_absolute_path(plans, roots, tmp_path):
    """An absolute log path in a manifest is treated as corrupt, not followed."""
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n")
    manifest = {"stories": {"S1": {"log": str(outside)}}}

    assert _reader().read_story_log("contract-plan", "S1", manifest) == {
        "available": False,
        "lines": [],
    }


def test_the_story_log_tail_is_clamped(plans, roots):
    """The tail length is clamped to its cap and never returns the whole file."""
    all_lines = [f"line {i}" for i in range(600)]
    _write(plans / "demo.S1.log", "\n".join(all_lines))
    manifest = _manifest("demo.S1.log")

    got = _reader().read_story_log("demo", "S1", manifest, lines=600)

    assert got["available"] is True
    assert len(got["lines"]) == 500
    assert got["lines"] == all_lines[-500:]


def test_the_story_log_defaults_to_200_lines_and_never_raises(plans, roots):
    """The default tail is 200 lines and malformed input degrades, never raises."""
    all_lines = [f"line {i}" for i in range(300)]
    _write(plans / "demo.S1.log", "\n".join(all_lines))

    got = _reader().read_story_log("demo", "S1", _manifest("demo.S1.log"))

    assert got["available"] is True
    assert len(got["lines"]) == 200

    for manifest in (
        None,
        {},
        {"stories": "nope"},
        {"stories": {"S1": "nope"}},
        {"stories": {"S1": {"log": None}}},
        {"stories": {"S1": {"log": ""}}},
    ):
        result = _reader().read_story_log("demo", "S1", manifest)
        assert result == {"available": False, "lines": []}


def test_the_story_log_decodes_non_utf8_bytes(plans, roots):
    """Non-UTF-8 bytes decode to replacement characters instead of raising."""
    log_path = plans / "demo.S1.log"
    log_path.write_bytes(b"good\xff\xfebad\n")

    got = _reader().read_story_log("demo", "S1", _manifest("demo.S1.log"))

    assert got["available"] is True
    assert "\ufffd" in "".join(got["lines"])


# ---------------------------------------------------------------------------
# the worktree-file read, now owned by the reader
# ---------------------------------------------------------------------------


def test_the_worktree_file_rejects_unsafe_input(roots):
    """Only a plain filename inside an absolute worktree is readable."""
    empty = {"available": False, "text": ""}

    assert _reader().read_worktree_file({}, "notes.md") == empty
    assert _reader().read_worktree_file("not-a-dict", "notes.md") == empty
    assert _reader().read_worktree_file({"worktree": "relative/path"}, "notes.md") == empty
    assert _reader().read_worktree_file({"worktree": "/tmp"}, "../escape") == empty
    assert _reader().read_worktree_file({"worktree": "/tmp"}, "") == empty


def test_the_worktree_file_reads_a_plain_file_inside_the_worktree(roots):
    """A plain filename inside the configured worktree root is served."""
    worktree = roots / "S1"
    _write(worktree / "notes.md", "hello\n")

    got = _reader().read_worktree_file({"worktree": str(worktree)}, "notes.md")

    assert got == {"available": True, "text": "hello\n"}
    assert _reader().read_worktree_file({"worktree": str(worktree)}, "absent.md")[
        "available"
    ] is False


def test_the_worktree_file_rejects_a_worktree_outside_the_root(roots, tmp_path):
    """A worktree path pointing outside WORKTREE_ROOT is rejected."""
    outside = tmp_path / "elsewhere"
    _write(outside / ".agent_plan.md", "secret\n")

    got = _reader().read_worktree_file({"worktree": str(outside)}, ".agent_plan.md")

    assert got == {"available": False, "text": ""}


def test_the_worktree_file_decodes_non_utf8_bytes(roots):
    """Non-UTF-8 bytes decode to replacement characters instead of raising."""
    worktree = roots / "S1"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".agent_plan.md").write_bytes(b"good\xff\xfebad")

    got = _reader().read_worktree_file({"worktree": str(worktree)}, ".agent_plan.md")

    assert got["available"] is True
    assert "\ufffd" in got["text"]


def test_the_worktree_file_never_raises_on_any_bad_input(roots):
    """Every malformed story/filename pair degrades to the unavailable body."""
    cases = [
        (None, ".agent_plan.md"),
        ({}, ""),
        ({}, ".agent_plan.md"),
        ({"worktree": ""}, ".agent_plan.md"),
        ({"worktree": "rel"}, ".agent_plan.md"),
        ({"worktree": str(roots / "S1")}, "../escape"),
        ({"worktree": str(roots / "S1")}, "a/b"),
        ({"worktree": "/etc"}, "passwd"),
    ]
    for story, filename in cases:
        try:
            result = _reader().read_worktree_file(story, filename)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"read_worktree_file({story!r},{filename!r}) raised {exc!r}"
            )
        assert result == {"available": False, "text": ""}
