"""Contract tests for the worktree_patch / worktree_patch_parse split (RH-14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import worktree_patch, worktree_patch_parse

REPO_ROOT = Path(__file__).resolve().parents[2]

MOVED_NAMES = [
    "PatchFormatError",
    "ParsedDiff",
    "ParsedHunk",
    "parse_unified_diff",
    "_c_unquote",
    "_consume_hunk_line",
    "_OpenHunk",
    "_HUNK_HEADER_RE",
    "_SIMPLE_C_ESCAPES",
]

VALID_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_should_expose_moved_name_from_new_module(name):
    assert hasattr(worktree_patch_parse, name)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_should_reexport_same_object_from_original_module(name):
    assert getattr(worktree_patch, name) is getattr(worktree_patch_parse, name)


def test_should_keep_public_moved_names_in_all():
    assert {"PatchFormatError", "parse_unified_diff"} <= set(worktree_patch.__all__)


def test_should_keep_patch_store_dict_in_original_module():
    assert isinstance(worktree_patch._PATCH_STORE, dict)
    assert not hasattr(worktree_patch_parse, "_PATCH_STORE")


def test_should_reach_patched_parse_unified_diff_from_validate_for_propose(
    monkeypatch, tmp_path
):
    calls = []

    def fake(diff_text):
        calls.append(diff_text)
        raise worktree_patch.PatchFormatError("patched parser reached")

    monkeypatch.setattr(worktree_patch, "parse_unified_diff", fake)

    with pytest.raises(worktree_patch.PatchFormatError, match="patched parser reached"):
        worktree_patch.validate_for_propose(VALID_DIFF, str(tmp_path))
    assert calls == [VALID_DIFF]


def test_should_still_reject_malformed_diff_via_original_module():
    with pytest.raises(worktree_patch.PatchFormatError):
        worktree_patch.parse_unified_diff("+++ b/x\n")


@pytest.mark.parametrize(
    "relative", ["pipeline/worktree_patch.py", "pipeline/worktree_patch_parse.py"]
)
def test_should_keep_file_under_1000_lines(relative):
    line_count = len((REPO_ROOT / relative).read_text().splitlines())
    assert line_count < 1000
