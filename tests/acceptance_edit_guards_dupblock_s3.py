"""Acceptance: duplicated_block_warning detects insertion-class damage.

New pure function with no call sites yet, so unit-level grading is
correct here; the wiring story has its own integration fixture.
"""
from pipeline import edit_guards


SHIPPED = "Shipped: PRs #214-217.\nSee the retro for detail.\n"


def test_real_regression_shape_is_flagged():
    new_str = "- [x] Notify operator\n" + SHIPPED
    surrounding = "## A3 item\n" + SHIPPED + "\nNext section.\n"
    warning = edit_guards.duplicated_block_warning(new_str, surrounding)
    assert warning
    assert "Shipped" in warning


def test_no_warning_when_nothing_is_duplicated():
    assert edit_guards.duplicated_block_warning(
        "brand new line\nanother new line\n", "## A3 item\n\nNext section.\n"
    ) == ""


def test_single_duplicated_line_is_below_the_threshold():
    assert edit_guards.duplicated_block_warning(
        "shared line\nunique line\n", "prefix\nshared line\nsuffix\n"
    ) == ""


def test_exactly_min_lines_triggers():
    assert edit_guards.duplicated_block_warning(
        "a\nb\n", "prefix\na\nb\nsuffix\n"
    ) != ""


def test_blank_run_is_not_a_duplicate():
    assert edit_guards.duplicated_block_warning("\n\n\n", "x\n\n\n\ny\n") == ""


def test_near_but_inexact_duplicate_is_not_flagged():
    assert edit_guards.duplicated_block_warning(
        "Shipped: PRs #214-217.\nSee the retro for detail.\n",
        "Shipped: PRs #214-218.\nSee the retros for detail.\n",
    ) == ""


def test_empty_inputs_are_safe():
    assert edit_guards.duplicated_block_warning("", "") == ""
    assert edit_guards.duplicated_block_warning("a\nb\n", "") == ""
    assert edit_guards.duplicated_block_warning("", "a\nb\n") == ""


def test_large_duplicate_is_capped():
    block = "".join(f"line_{i}\n" for i in range(200))
    warning = edit_guards.duplicated_block_warning(block, "head\n" + block + "tail\n")
    assert warning
    assert len(warning) < 3000
    assert "truncat" in warning.lower() or "more line" in warning.lower()


def test_indentation_is_significant():
    assert edit_guards.duplicated_block_warning(
        "    a\n    b\n", "a\nb\n"
    ) == ""


def test_min_lines_is_configurable():
    assert edit_guards.duplicated_block_warning(
        "a\nb\nc\n", "x\na\nb\nc\ny\n", min_lines=4
    ) == ""
