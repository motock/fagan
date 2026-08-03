"""Acceptance: verify_range_anchors validates optional boundary
expectations without ever raising.

New pure function with no call sites yet; the wiring story grades the
integration separately.
"""
from pipeline import edit_guards

LINES = ["def f():\n", "    a = 1\n", "    b = 2\n", "    return a\n"]


def test_no_expectations_means_nothing_to_verify():
    assert edit_guards.verify_range_anchors(LINES, 2, 3, None, None) == ""


def test_both_anchors_matching_passes():
    assert edit_guards.verify_range_anchors(
        LINES, 2, 3, "    a = 1", "    b = 2"
    ) == ""


def test_first_anchor_only_matching_passes():
    assert edit_guards.verify_range_anchors(LINES, 2, 3, "    a = 1", None) == ""


def test_mismatched_first_anchor_is_reported():
    msg = edit_guards.verify_range_anchors(LINES, 2, 3, "    a = 99", None)
    assert msg
    assert "a = 1" in msg


def test_mismatched_last_anchor_is_reported():
    msg = edit_guards.verify_range_anchors(
        LINES, 2, 3, "    a = 1", "    zzz"
    )
    assert msg


def test_stale_range_suggests_where_the_text_actually_is():
    msg = edit_guards.verify_range_anchors(LINES, 1, 1, "    b = 2", None)
    assert "3" in msg


def test_trailing_whitespace_and_newline_differences_are_tolerated():
    assert edit_guards.verify_range_anchors(
        LINES, 2, 2, "    a = 1   \n", None
    ) == ""


def test_leading_indentation_difference_is_not_tolerated():
    assert edit_guards.verify_range_anchors(LINES, 2, 2, "a = 1", None) != ""


def test_single_line_range():
    assert edit_guards.verify_range_anchors(
        LINES, 2, 2, "    a = 1", "    a = 1"
    ) == ""


def test_out_of_bounds_start_returns_a_message_not_an_exception():
    assert edit_guards.verify_range_anchors(LINES, 99, 99, "x", None) != ""


def test_out_of_bounds_end_returns_a_message_not_an_exception():
    assert edit_guards.verify_range_anchors(LINES, 1, 99, None, "x") != ""


def test_expected_text_absent_from_file_still_reports():
    msg = edit_guards.verify_range_anchors(LINES, 2, 2, "nowhere at all", None)
    assert msg


def test_suggestions_are_capped_at_three():
    lines = ["dup\n"] * 10 + ["other\n"]
    msg = edit_guards.verify_range_anchors(lines, 11, 11, "dup", None)
    assert msg
    assert msg.count("dup") <= 6


def test_empty_file_is_safe():
    assert edit_guards.verify_range_anchors([], 1, 1, "x", None) != ""
