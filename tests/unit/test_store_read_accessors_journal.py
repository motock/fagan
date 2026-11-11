"""Tests for the GROUP 2 Store read accessors (journal / story-log / worktree-file).

These mirror the safety logic that lives in ``app/dashboard.py``'s
``_read_journal`` / ``_journal_final_ts`` / ``_read_story_log`` /
``_read_worktree_file`` helpers and assert that the new
``FileStore``/``PipelineService`` seams replicate that logic byte-for-byte.

The story-log and worktree-file reads moved to the ``WorkspaceReader`` seam
(ADR 0012 decision 1): the store no longer carries them, so those cases now
live in ``tests/unit/test_workspace_reader.py`` and
``tests/unit/test_store_no_workspace_reads.py``. What stays here is the
journal accessor surface of the store and the service.
"""

import json
from pathlib import Path

import pytest

from pipeline.server import FileStore, PipelineService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect pipeline.server.PLAN_DIR to a tmp dir and return it."""
    from pipeline import server

    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    """Redirect pipeline.server.WORKTREE_ROOT to a tmp dir and return it."""
    from pipeline import server

    wt = tmp_path / "worktrees"
    wt.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "WORKTREE_ROOT", wt)
    return wt


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# get_journal
# ---------------------------------------------------------------------------

class TestGetJournal:
    def test_happy_path_returns_entries(self, plan_dir):
        plan, story = "p1", "S1"
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "step": "step-a", "summary": "did a", "next_hint": "do b"},
            {"ts": "2026-01-01T00:01:00Z", "step": "step-b", "summary": "did b"},
        ]
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps(entries))
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is True
        assert isinstance(got, list)
        assert len(got) == 2
        # file order preserved
        assert got[0]["ts"] == entries[0]["ts"]
        assert got[1]["ts"] == entries[1]["ts"]

    def test_normalizes_optional_fields_to_none(self, plan_dir):
        """Entries missing step/summary/next_hint must still expose those keys
        normalized to None (mirrors _read_journal)."""
        plan, story = "p1", "S1"
        entries = [{"ts": "t1"}]  # no step/summary/next_hint
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps(entries))
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is True
        assert got[0]["step"] is None
        assert got[0]["summary"] is None
        assert got[0]["next_hint"] is None
        # original keys preserved
        assert got[0]["ts"] == "t1"

    def test_missing_file_returns_false_empty(self, plan_dir):
        store = FileStore()
        available, got = store.get_journal("nope", "S1")
        assert available is False
        assert got == []

    def test_malformed_json_returns_false_empty(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", "{not json")
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is False
        assert got == []

    def test_empty_list_returns_false_empty(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", "[]")
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is False
        assert got == []

    def test_non_dict_rows_filtered_out(self, plan_dir):
        """A stray non-object row must not crash; only dicts are kept, and if
        none remain the result is (False, [])."""
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps(["stray", 42, None]))
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is False
        assert got == []

    def test_mixed_dict_and_non_dict_keeps_dicts(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(
            plan_dir / f"{plan}.{story}.journal.json",
            json.dumps([{"ts": "t1"}, "junk", {"ts": "t2"}]),
        )
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is True
        assert len(got) == 2
        assert got[0]["ts"] == "t1"
        assert got[1]["ts"] == "t2"

    def test_non_json_top_level_object_returns_false(self, plan_dir):
        """A JSON object (not a list) at top level -> (False, [])."""
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps({"ts": "t1"}))
        store = FileStore()
        available, got = store.get_journal(plan, story)
        assert available is False
        assert got == []

    def test_non_utf8_bytes_do_not_raise(self, plan_dir):
        plan, story = "p1", "S1"
        _write_bytes(plan_dir / f"{plan}.{story}.journal.json", b"\x80\x81\xff")
        store = FileStore()
        # Must not raise; malformed bytes -> (False, [])
        available, got = store.get_journal(plan, story)
        assert available is False
        assert got == []

    def test_never_raises_on_any_bad_input(self, plan_dir):
        """Negative sweep: no combination of missing/corrupt input raises."""
        store = FileStore()
        for plan, story in [("", ""), ("x", ""), ("", "y"), ("a", "b")]:
            try:
                store.get_journal(plan, story)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"get_journal({plan!r},{story!r}) raised {exc!r}")


# ---------------------------------------------------------------------------
# get_journal_final_ts
# ---------------------------------------------------------------------------

class TestGetJournalFinalTs:
    def test_happy_path_returns_last_ts(self, plan_dir):
        plan, story = "p1", "S1"
        entries = [
            {"ts": "2026-01-01T00:00:00Z"},
            {"ts": "2026-01-01T00:01:00Z"},
        ]
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps(entries))
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) == "2026-01-01T00:01:00Z"

    def test_missing_file_returns_none(self, plan_dir):
        store = FileStore()
        assert store.get_journal_final_ts("nope", "S1") is None

    def test_malformed_json_returns_none(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", "{bad")
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_empty_list_returns_none(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", "[]")
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_non_list_returns_none(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps({"ts": "t"}))
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_final_entry_not_dict_returns_none(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps(["stray"]))
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_final_entry_missing_ts_returns_none(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps([{"no_ts": "x"}]))
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_non_utf8_does_not_raise(self, plan_dir):
        plan, story = "p1", "S1"
        _write_bytes(plan_dir / f"{plan}.{story}.journal.json", b"\x80\x81")
        store = FileStore()
        assert store.get_journal_final_ts(plan, story) is None

    def test_never_raises_on_any_bad_input(self, plan_dir):
        store = FileStore()
        for plan, story in [("", ""), ("x", ""), ("", "y"), ("a", "b")]:
            try:
                store.get_journal_final_ts(plan, story)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"get_journal_final_ts({plan!r},{story!r}) raised {exc!r}")


# ---------------------------------------------------------------------------
# The story-log and worktree-file reads moved to the WorkspaceReader seam
# (ADR 0012 decision 1); they are asserted in
# tests/unit/test_workspace_reader.py and tests/unit/test_store_no_workspace_reads.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PipelineService delegators
# ---------------------------------------------------------------------------

class TestPipelineServiceDelegators:
    def test_delegates_get_journal(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps([{"ts": "t1"}]))
        svc = PipelineService()
        available, got = svc.get_journal(plan, story)
        assert available is True
        assert got[0]["ts"] == "t1"

    def test_delegates_get_journal_final_ts(self, plan_dir):
        plan, story = "p1", "S1"
        _write_text(plan_dir / f"{plan}.{story}.journal.json", json.dumps([{"ts": "t1"}, {"ts": "t2"}]))
        svc = PipelineService()
        assert svc.get_journal_final_ts(plan, story) == "t2"

    def test_delegates_get_story_log(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        _write_text(log_path, "a\nb\nc\n")
        manifest = {"stories": {story: {"log": f"{plan}.{story}.log"}}}
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        svc = PipelineService()
        result = svc.get_story_log(plan, story, manifest, lines=2)
        assert result["available"] is True
        assert result["lines"] == ["b", "c"]

    def test_delegates_get_worktree_file(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        _write_text(wt / ".agent_plan.md", "body\n")
        story = {"worktree": str(wt)}
        svc = PipelineService()
        result = svc.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is True
        assert result["text"] == "body\n"

    def test_delegators_exist_on_pipeline_service(self):
        """All four GROUP-2 accessors must be callable methods on PipelineService."""
        svc = PipelineService()
        for name in ("get_journal", "get_journal_final_ts", "get_story_log", "get_worktree_file"):
            assert callable(getattr(svc, name)), f"PipelineService.{name} missing"

    def test_delegators_exist_on_file_store(self):
        """The journal accessors stay on FileStore; the workspace reads do not."""
        store = FileStore()
        for name in ("get_journal", "get_journal_final_ts"):
            assert callable(getattr(store, name)), f"FileStore.{name} missing"
        for name in ("get_story_log", "get_worktree_file"):
            assert not hasattr(store, name), f"FileStore still exposes {name}"


# ---------------------------------------------------------------------------
# Abstract Store seam declares the new accessors
# ---------------------------------------------------------------------------

class TestStoreProtocolDeclaresAccessors:
    def test_abstract_store_declares_get_journal(self):
        """The abstract Store base class (the seam) must declare the journal
        accessors so a custom Store implementation is forced to provide them."""
        from pipeline.server import Store  # the abstract base

        for name in ("get_journal", "get_journal_final_ts"):
            assert hasattr(Store, name), f"Store abstract base missing {name}"

    def test_abstract_store_declares_no_workspace_read(self):
        """The abstract Store base class must not declare a workspace read."""
        from pipeline.server import Store  # the abstract base

        for name in ("get_story_log", "get_worktree_file"):
            assert not hasattr(Store, name), f"Store abstract base still has {name}"
