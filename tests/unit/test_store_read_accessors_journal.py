"""Tests for the GROUP 2 Store read accessors (journal / story-log / worktree-file).

These mirror the safety logic that lives in ``app/dashboard.py``'s
``_read_journal`` / ``_journal_final_ts`` / ``_read_story_log`` /
``_read_worktree_file`` helpers and assert that the new
``FileStore``/``PipelineService`` seams replicate that logic byte-for-byte.

The implementation does not exist yet (this is the RED test file written
first); every accessor below should fail with an AttributeError/ImportError
until ``pipeline/server.py`` grows the methods.
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
# get_story_log
# ---------------------------------------------------------------------------

class TestGetStoryLog:
    def _manifest_with_log(self, plan_dir, story_key, log_value):
        """Write a manifest whose stories[story_key]['log'] = log_value."""
        manifest = {"stories": {story_key: {"log": log_value}}}
        _write_text(plan_dir / f"{plan_dir.name}.manifest.json", json.dumps(manifest))
        return manifest

    def test_happy_path_returns_tail(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        all_lines = [f"line {i}" for i in range(10)]
        _write_text(log_path, "\n".join(all_lines))
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest, lines=5)
        assert result["available"] is True
        assert result["lines"] == all_lines[-5:]

    def test_default_lines_is_200(self, plan_dir):
        """When lines is omitted, the default tail is 200 lines."""
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        all_lines = [f"line {i}" for i in range(300)]
        _write_text(log_path, "\n".join(all_lines))
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is True
        assert len(result["lines"]) == 200
        assert result["lines"] == all_lines[-200:]

    def test_lines_clamped_to_cap_500(self, plan_dir):
        """lines > _LOG_TAIL_CAP (500) is clamped to 500."""
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        all_lines = [f"line {i}" for i in range(600)]
        _write_text(log_path, "\n".join(all_lines))
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest, lines=600)
        assert result["available"] is True
        assert len(result["lines"]) == 500
        assert result["lines"] == all_lines[-500:]

    def test_lines_zero_falls_back_to_default(self, plan_dir):
        """lines < 1 is coerced to the default (200), not 0/all."""
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        all_lines = [f"line {i}" for i in range(300)]
        _write_text(log_path, "\n".join(all_lines))
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest, lines=0)
        assert result["available"] is True
        assert len(result["lines"]) == 200

    def test_missing_log_file_returns_unavailable(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_missing_log_field_returns_unavailable(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        manifest = {"stories": {story: {}}}  # no 'log' key
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_empty_log_field_returns_unavailable(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        manifest = {"stories": {story: {"log": ""}}}
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_story_key_missing_from_manifest_returns_unavailable(self, plan_dir):
        plan, story = plan_dir.name, "S1"
        manifest = {"stories": {"other": {"log": "x.log"}}}
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_manifest_not_dict_returns_unavailable(self, plan_dir):
        store = FileStore()
        result = store.get_story_log("p", "S1", manifest=None)
        assert result["available"] is False
        assert result["lines"] == []

    def test_manifest_stories_not_dict_returns_unavailable(self, plan_dir):
        store = FileStore()
        result = store.get_story_log("p", "S1", manifest={"stories": "nope"})
        assert result["available"] is False
        assert result["lines"] == []

    def test_story_not_dict_returns_unavailable(self, plan_dir):
        store = FileStore()
        result = store.get_story_log("p", "S1", manifest={"stories": {"S1": "nope"}})
        assert result["available"] is False
        assert result["lines"] == []

    def test_log_path_outside_plan_dir_returns_unavailable(self, plan_dir):
        """A manifest log path pointing outside PLAN_DIR (e.g. ../outside.log)
        must degrade to available=False, never read the file."""
        plan, story = plan_dir.name, "S1"
        # write a file just outside plan_dir
        outside = plan_dir.parent / "outside.log"
        _write_text(outside, "secret\n")
        manifest = {"stories": {story: {"log": "../outside.log"}}}
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_absolute_log_path_outside_plan_dir_returns_unavailable(self, plan_dir, tmp_path):
        """An absolute log path outside PLAN_DIR must also be rejected."""
        plan, story = plan_dir.name, "S1"
        outside = tmp_path / "elsewhere.log"
        _write_text(outside, "secret\n")
        manifest = {"stories": {story: {"log": str(outside)}}}
        _write_text(plan_dir / f"{plan}.manifest.json", json.dumps(manifest))
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is False
        assert result["lines"] == []

    def test_non_utf8_log_decodes_with_replacement_chars(self, plan_dir):
        """Non-UTF-8 bytes must decode to U+FFFD replacement chars, not raise."""
        plan, story = plan_dir.name, "S1"
        log_path = plan_dir / f"{plan}.{story}.log"
        _write_bytes(log_path, b"good\xff\xfebad\n")
        manifest = self._manifest_with_log(plan_dir, story, f"{plan}.{story}.log")
        store = FileStore()
        result = store.get_story_log(plan, story, manifest)
        assert result["available"] is True
        joined = "".join(result["lines"])
        # replacement char U+FFFD must appear, not an exception
        assert "\ufffd" in joined

    def test_never_raises_on_any_bad_input(self, plan_dir):
        store = FileStore()
        cases = [
            ("", "", None),
            ("p", "", {}),
            ("p", "S1", {"stories": "x"}),
            ("p", "S1", {"stories": {"S1": "x"}}),
            ("p", "S1", {"stories": {"S1": {"log": "../../etc/passwd"}}}),
            ("p", "S1", {"stories": {"S1": {"log": None}}}),
        ]
        for plan, story, manifest in cases:
            try:
                store.get_story_log(plan, story, manifest)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"get_story_log({plan!r},{story!r},{manifest!r}) raised {exc!r}")


# ---------------------------------------------------------------------------
# get_worktree_file
# ---------------------------------------------------------------------------

class TestGetWorktreeFile:
    def test_happy_path_returns_text(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        _write_text(wt / ".agent_plan.md", "# plan body\n")
        story = {"worktree": str(wt)}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is True
        assert result["text"] == "# plan body\n"

    def test_missing_file_returns_unavailable(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        story = {"worktree": str(wt)}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_missing_worktree_dir_returns_unavailable(self, plan_dir, worktree_root):
        story = {"worktree": str(worktree_root / "gone")}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_story_not_dict_returns_unavailable(self, plan_dir, worktree_root):
        store = FileStore()
        result = store.get_worktree_file("not-a-dict", ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_story_missing_worktree_returns_unavailable(self, plan_dir, worktree_root):
        store = FileStore()
        result = store.get_worktree_file({}, ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_empty_filename_returns_unavailable(self, plan_dir, worktree_root):
        story = {"worktree": str(worktree_root / "S1")}
        store = FileStore()
        result = store.get_worktree_file(story, "")
        assert result["available"] is False
        assert result["text"] == ""

    def test_filename_with_slash_rejected(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        _write_text(wt / "sibling.txt", "sibling\n")
        story = {"worktree": str(wt)}
        store = FileStore()
        result = store.get_worktree_file(story, "sibling.txt")  # plain ok
        assert result["available"] is True
        # but a path separator must be rejected
        result = store.get_worktree_file(story, "sub/dir.txt")
        assert result["available"] is False
        assert result["text"] == ""

    def test_filename_with_backslash_rejected(self, plan_dir, worktree_root):
        story = {"worktree": str(worktree_root / "S1")}
        store = FileStore()
        result = store.get_worktree_file(story, "sub\\dir.txt")
        assert result["available"] is False
        assert result["text"] == ""

    def test_filename_dot_dot_rejected(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        # a sibling worktree exists
        sib = worktree_root / "S2"
        sib.mkdir(parents=True, exist_ok=True)
        _write_text(sib / "secret.txt", "secret\n")
        story = {"worktree": str(wt)}
        store = FileStore()
        # ".." filename must not escape to the sibling
        result = store.get_worktree_file(story, "..")
        assert result["available"] is False
        assert result["text"] == ""

    def test_filename_dot_rejected(self, plan_dir, worktree_root):
        story = {"worktree": str(worktree_root / "S1")}
        store = FileStore()
        result = store.get_worktree_file(story, ".")
        assert result["available"] is False
        assert result["text"] == ""

    def test_relative_worktree_rejected(self, plan_dir, worktree_root):
        """A relative worktree path is a corrupt manifest -> fail closed."""
        story = {"worktree": "relative/path"}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_worktree_outside_worktree_root_rejected(self, plan_dir, worktree_root, tmp_path):
        """A worktree path pointing outside WORKTREE_ROOT must be rejected."""
        outside = tmp_path / "elsewhere"
        outside.mkdir(parents=True, exist_ok=True)
        _write_text(outside / ".agent_plan.md", "secret\n")
        story = {"worktree": str(outside)}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is False
        assert result["text"] == ""

    def test_non_utf8_file_decodes_with_replacement_chars(self, plan_dir, worktree_root):
        story_key = "S1"
        wt = worktree_root / story_key
        wt.mkdir(parents=True, exist_ok=True)
        _write_bytes(wt / ".agent_plan.md", b"good\xff\xfebad")
        story = {"worktree": str(wt)}
        store = FileStore()
        result = store.get_worktree_file(story, ".agent_plan.md")
        assert result["available"] is True
        assert "\ufffd" in result["text"]

    def test_never_raises_on_any_bad_input(self, plan_dir, worktree_root):
        store = FileStore()
        cases = [
            (None, ".agent_plan.md"),
            ({}, ""),
            ({}, ".agent_plan.md"),
            ({"worktree": ""}, ".agent_plan.md"),
            ({"worktree": "rel"}, ".agent_plan.md"),
            ({"worktree": str(worktree_root / "S1")}, "../escape"),
            ({"worktree": str(worktree_root / "S1")}, "a/b"),
            ({"worktree": "/etc"}, "passwd"),
        ]
        for story, filename in cases:
            try:
                store.get_worktree_file(story, filename)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"get_worktree_file({story!r},{filename!r}) raised {exc!r}")


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
        """All four GROUP-2 accessors must be callable methods on FileStore."""
        store = FileStore()
        for name in ("get_journal", "get_journal_final_ts", "get_story_log", "get_worktree_file"):
            assert callable(getattr(store, name)), f"FileStore.{name} missing"


# ---------------------------------------------------------------------------
# Abstract Store seam declares the new accessors
# ---------------------------------------------------------------------------

class TestStoreProtocolDeclaresAccessors:
    def test_abstract_store_declares_get_journal(self):
        """The abstract Store base class (the seam) must declare all four
        GROUP-2 accessors so a custom Store implementation is forced to
        provide them."""
        from pipeline.server import Store  # the abstract base

        for name in ("get_journal", "get_journal_final_ts", "get_story_log", "get_worktree_file"):
            assert hasattr(Store, name), f"Store abstract base missing {name}"