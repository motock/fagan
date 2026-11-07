"""Failure and escape paths of ``parse_unified_diff`` that no other test drives.

The parser sits on the patch-apply deny boundary: the path it reports is the
one the deny rules (``CLAUDE.md`` and friends) are checked against, so a
decoding mistake in git's C-quoted path syntax is a way around the deny list.
Everything here goes through the public ``parse_unified_diff``.
"""

from __future__ import annotations

import pytest

from pipeline.worktree_patch_parse import PatchFormatError, parse_unified_diff

HUNK = "@@ -1 +1 @@\n-old\n+new\n"


def _diff_with_new_path(token: str) -> str:
    return f"--- a/old.py\n+++ {token}\n{HUNK}"


def _new_path(token: str) -> str:
    return parse_unified_diff(_diff_with_new_path(token)).paths[0]


# ---------------------------------------------------------------------------
# C-quoted path decoding: positive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escape", "decoded"),
    [
        ("\\a", "\a"),
        ("\\b", "\b"),
        ("\\f", "\f"),
        ("\\n", "\n"),
        ("\\r", "\r"),
        ("\\t", "\t"),
        ("\\v", "\v"),
        ("\\\\", "\\"),
    ],
)
def test_should_decode_simple_c_escape_in_quoted_path(escape: str, decoded: str) -> None:
    assert _new_path(f'"b/x{escape}y"') == f"x{decoded}y"


@pytest.mark.parametrize(
    ("octal", "decoded"),
    [
        ("\\056", "."),
        ("\\101", "A"),
        ("\\60", "0"),
        ("\\7", "\x07"),
    ],
)
def test_should_decode_octal_escape_of_one_to_three_digits(octal: str, decoded: str) -> None:
    assert _new_path(f'"b/x{octal}y"') == f"x{decoded}y"


def test_should_stop_octal_escape_after_three_digits() -> None:
    assert _new_path('"b/\\1012"') == "A2"


def test_should_report_octal_spelled_deny_listed_name_in_its_plain_form() -> None:
    # "\056" is "." -- a patch spelling CLAUDE.md this way must be reported as
    # CLAUDE.md so the deny rules see the file git would actually write.
    assert parse_unified_diff(_diff_with_new_path('"b/CLAUDE\\056md"')).paths == ["CLAUDE.md"]


def test_should_report_plain_unquoted_path_unchanged() -> None:
    assert _new_path("b/dir/file.py") == "dir/file.py"


# ---------------------------------------------------------------------------
# C-quoted path decoding: negative (fail closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        '"b/x\\"',  # dangling backslash before the closing quote
        '"b/x\\qy"',  # unknown escape letter
        '"b/x\\8y"',  # 8 is not an octal digit
        '"b/x\\"y"',  # escaped quote: an inner quote is refused, never decoded
        '"b/x"y"',  # stray inner quote
        '"b/x"trailing',  # garbage after the closing quote
    ],
)
def test_should_refuse_malformed_quoted_new_path(token: str) -> None:
    with pytest.raises(PatchFormatError, match="unparseable quoted path"):
        parse_unified_diff(_diff_with_new_path(token))


def test_should_refuse_malformed_quoted_old_path() -> None:
    diff = f'--- "a/x\\q"\n+++ b/x\n{HUNK}'

    with pytest.raises(PatchFormatError, match="unparseable quoted path"):
        parse_unified_diff(diff)


# ---------------------------------------------------------------------------
# Structure errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("not_text", [None, b"--- a/x\n", 123, ["--- a/x"]])
def test_should_refuse_non_string_diff(not_text: object) -> None:
    with pytest.raises(PatchFormatError, match="diff text must be a string"):
        parse_unified_diff(not_text)  # type: ignore[arg-type]


def test_should_refuse_unknown_prefix_inside_an_open_hunk() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n context\nXbad\n"

    with pytest.raises(PatchFormatError, match="unexpected hunk body line"):
        parse_unified_diff(diff)


def test_should_refuse_two_old_headers_in_a_row() -> None:
    with pytest.raises(PatchFormatError, match="incomplete file block"):
        parse_unified_diff("--- a/x\n--- a/y\n")


def test_should_refuse_a_file_block_with_no_hunk_before_the_next_file() -> None:
    diff = f"--- a/x\n+++ b/x\n--- a/y\n+++ b/y\n{HUNK}"

    with pytest.raises(PatchFormatError, match="incomplete file block"):
        parse_unified_diff(diff)


def test_should_refuse_a_file_block_with_no_hunk_at_end_of_input() -> None:
    with pytest.raises(PatchFormatError, match="incomplete file block"):
        parse_unified_diff("--- a/x\n+++ b/x\n")


def test_should_refuse_a_new_header_without_a_preceding_old_header() -> None:
    with pytest.raises(PatchFormatError, match="without a preceding"):
        parse_unified_diff(f"+++ b/x\n{HUNK}")


def test_should_refuse_a_hunk_header_outside_a_file_block() -> None:
    with pytest.raises(PatchFormatError, match="outside a"):
        parse_unified_diff(HUNK)


def test_should_refuse_a_hunk_that_ends_before_its_declared_count() -> None:
    with pytest.raises(PatchFormatError, match="fewer lines"):
        parse_unified_diff("--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-only\n")


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


def test_should_accept_a_degenerate_empty_hunk() -> None:
    parsed = parse_unified_diff("--- a/x\n+++ b/x\n@@ -0,0 +0,0 @@\n")

    assert (parsed.paths, len(parsed.hunks), parsed.added_lines) == (["x"], 1, 0)
