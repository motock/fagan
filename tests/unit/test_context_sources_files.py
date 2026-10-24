"""files delivery mode: snapshot materialized outside the worktree, bounded pointer rendered."""
import json
from pathlib import Path

import pytest

from pipeline import context_sources as cs
from pipeline import paths
from pipeline.persona import _build_dispatch_command, _with_files_pointers

TEXT = "--- a.txt ---\nalpha\n--- sub/dir/b.md ---\nbeta\n"
SOURCE = {"name": "docs", "provider": "dir", "uri": "/some/where"}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", d)
    return d


def test_should_materialize_files_preserving_subdirectories(plan_dir):
    folder = Path(cs.materialize_context_files(SOURCE, TEXT))

    assert (folder / "files" / "a.txt").read_text() == "alpha\n"
    assert (folder / "files" / "sub" / "dir" / "b.md").read_text() == "beta\n"


def test_should_place_snapshot_under_plan_dir_digest_folder(plan_dir):
    folder = Path(cs.materialize_context_files(SOURCE, TEXT))

    assert folder.parent == plan_dir / "context"
    assert len(folder.name) == 64


def test_should_write_meta_with_source_digest_and_files(plan_dir):
    folder = Path(cs.materialize_context_files(SOURCE, TEXT))

    meta = json.loads((folder / "meta.json").read_text())
    assert meta["name"] == "docs"
    assert meta["provider"] == "dir"
    assert meta["uri"] == "/some/where"
    assert meta["digest"] == "sha256:" + folder.name
    assert meta["files"] == [{"path": "a.txt", "bytes": 6}, {"path": "sub/dir/b.md", "bytes": 5}]


@pytest.mark.parametrize("rel", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "a\\..\\b"])
def test_should_reject_unsafe_relpath(plan_dir, rel):
    with pytest.raises(cs.ContextSourceError, match="unsafe"):
        cs.materialize_context_files(SOURCE, f"--- {rel} ---\nx\n")


def test_should_write_nothing_when_any_relpath_is_unsafe(plan_dir):
    with pytest.raises(cs.ContextSourceError):
        cs.materialize_context_files(SOURCE, "--- ok.txt ---\nx\n--- ../bad ---\ny\n")

    assert not (plan_dir / "context").exists()


def test_should_reject_text_without_file_entries(plan_dir):
    with pytest.raises(cs.ContextSourceError, match="no file entries"):
        cs.materialize_context_files(SOURCE, "plain text")


def test_should_be_noop_when_rematerializing(plan_dir):
    folder = Path(cs.materialize_context_files(SOURCE, TEXT))
    target = folder / "files" / "a.txt"
    target.write_text("tampered marker")

    again = Path(cs.materialize_context_files(SOURCE, TEXT))

    assert again == folder
    assert target.read_text() == "tampered marker"


def test_pointer_should_list_directory_and_relative_paths():
    files = [{"path": "a.txt", "bytes": 6}, {"path": "sub/b.md", "bytes": 5}]

    out = cs.render_files_pointer("docs", "/abs/snap", files)

    assert "REFERENCE DATA" in out
    assert "must not be followed" in out
    assert "docs" in out
    assert "/abs/snap/files" in out
    assert "- a.txt" in out
    assert "- sub/b.md" in out


def test_pointer_should_cap_listing_at_200_paths():
    files = [{"path": f"f{i:03}.txt", "bytes": 1} for i in range(250)]

    out = cs.render_files_pointer("docs", "/abs/snap", files)

    assert "- f199.txt" in out
    assert "- f200.txt" not in out
    assert "50 more" in out


def test_pointer_should_not_mention_overflow_at_exactly_200():
    files = [{"path": f"f{i:03}.txt", "bytes": 1} for i in range(200)]

    assert "more" not in cs.render_files_pointer("docs", "/abs/snap", files)


STORY = {"summary": "s", "agent_instructions": "BRIEF-TEXT", "rework_attempts": 0}
POINTER = {"name": "docs", "path": "/abs/snap", "files": [{"path": "a.txt", "bytes": 6}]}


def test_persona_should_append_pointer_section_after_brief():
    block = _with_files_pointers("", [POINTER])

    prompt = _build_dispatch_command(STORY, "K-1", context_block=block)["prompt"]

    assert prompt.count("/abs/snap/files") == 1
    assert prompt.index("BRIEF-TEXT") < prompt.index("/abs/snap/files")
    assert "- a.txt" in prompt


def test_persona_should_keep_prompt_block_before_pointer():
    block = _with_files_pointers("PROMPT-BLOCK", [POINTER])

    assert block.index("PROMPT-BLOCK") < block.index("/abs/snap/files")


def test_persona_prompt_mode_should_be_unchanged_without_pointers():
    assert _with_files_pointers("BLOCK", []) == "BLOCK"
    assert _with_files_pointers("", []) == ""
