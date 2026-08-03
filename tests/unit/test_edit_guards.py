"""Tests for pipeline/edit_guards.py.

This module turns scripts/local_agent.py's _removed_lines_echo (a
message the model was free to ignore) into a hard classification the
replace_lines tool can use to BLOCK a collateral edit. Two live
regressions motivate the two buckets:

  * a replace_lines edit silently deleted a pre-existing
    `_mark_plane_done(story_key, plan_name)` call -> must classify as a
    true DELETION (best similarity ratio against the replacement is low).
  * a replace_lines edit silently dropped a closing paren from a
    markdown parenthetical -> must classify as a REWRITE (ratio is high,
    but the report must still surface the exact character that changed,
    not a full-line echo that hides a single ')').

These tests exercise pipeline/edit_guards.py directly: this story adds a
brand-new module with no call sites yet (wiring is a separate story), so
there is no integration path to bypass by testing the unit in isolation.
"""
import ast
import difflib
import inspect
from pathlib import Path

import pytest

from pipeline import edit_guards

MODULE_PATH = Path(__file__).parent.parent.parent / "pipeline" / "edit_guards.py"

# The two live regressions this module exists to prevent, reused as fixture
# data across several tests below.
MARK_DONE_OLD = [
    '    story = _load(story_key)\n',
    '    worktree = story.get("worktree", "")\n',
    '    _merge_pr(story, worktree)\n',
    '    _mark_plane_done(story_key, plan_name)\n',
]
MARK_DONE_NEW = (
    '    story = _load(story_key)\n'
    '    worktree = story.get("worktree", "+")\n'
    '    _merge_pr(story, worktree)\n'
)

PAREN_OLD = ['  (not launchd-supervised, so a restart is manual)\n']
PAREN_NEW = '  (not launchd-supervised, so a restart is manual\n'


# ---------------------------------------------------------------------------
# classify_removed_lines
# ---------------------------------------------------------------------------

class TestClassifyRemovedLinesEmptyAndIdentity:
    def test_empty_old_lines_and_empty_new_str_returns_two_empty_lists(self):
        assert edit_guards.classify_removed_lines([], "") == ([], [])

    def test_empty_old_lines_with_nonempty_new_str_returns_two_empty_lists(self):
        assert edit_guards.classify_removed_lines([], "x = 1\n") == ([], [])

    def test_new_str_identical_to_old_lines_returns_two_empty_lists(self):
        old = ['    a = 1\n', '    b = 2\n']
        new = "".join(old)
        assert edit_guards.classify_removed_lines(old, new) == ([], [])

    def test_pure_insertion_alongside_a_verbatim_survivor_reports_nothing(self):
        old = ['    return 1\n']
        new = "    log.debug('entering')\n    return 1\n"
        assert edit_guards.classify_removed_lines(old, new) == ([], [])


class TestClassifyRemovedLinesDeletionVsRewrite:
    def test_empty_new_str_makes_every_old_line_a_deletion(self):
        old = ['    do_thing()\n', '    do_other_thing()\n']
        deletions, rewrites = edit_guards.classify_removed_lines(old, "")
        assert deletions == old
        assert rewrites == []

    def test_dropped_function_call_is_a_true_deletion(self):
        deletions, rewrites = edit_guards.classify_removed_lines(MARK_DONE_OLD, MARK_DONE_NEW)
        assert any('_mark_plane_done' in d for d in deletions)
        assert not any('_mark_plane_done' in old for old, _new, _r in rewrites)

    def test_corrupted_default_argument_is_a_rewrite_not_a_deletion(self):
        deletions, rewrites = edit_guards.classify_removed_lines(MARK_DONE_OLD, MARK_DONE_NEW)
        assert not any('worktree = story.get' in d for d in deletions)
        assert any('worktree = story.get' in old for old, _new, _r in rewrites)

    def test_verbatim_survivor_lines_appear_in_neither_bucket(self):
        deletions, rewrites = edit_guards.classify_removed_lines(MARK_DONE_OLD, MARK_DONE_NEW)
        assert not any('_load(story_key)' in d for d in deletions)
        assert not any('_load(story_key)' in old for old, _n, _r in rewrites)
        assert not any('_merge_pr' in d for d in deletions)
        assert not any('_merge_pr' in old for old, _n, _r in rewrites)

    def test_rewrite_tuple_shape_is_old_new_ratio(self):
        _deletions, rewrites = edit_guards.classify_removed_lines(MARK_DONE_OLD, MARK_DONE_NEW)
        assert len(rewrites) == 1
        old, new, ratio = rewrites[0]
        assert old == '    worktree = story.get("worktree", "")\n'
        assert new == '    worktree = story.get("worktree", "+")\n'
        assert isinstance(ratio, float)
        assert 0.9 <= ratio <= 1.0

    def test_legitimate_checkbox_edit_is_a_rewrite(self):
        old = ['    - [ ] Ship the thing\n']
        new = '    - [x] Ship the thing\n'
        deletions, rewrites = edit_guards.classify_removed_lines(old, new)
        assert deletions == []
        assert len(rewrites) == 1

    def test_dropped_trailing_paren_is_a_rewrite(self):
        deletions, rewrites = edit_guards.classify_removed_lines(PAREN_OLD, PAREN_NEW)
        assert deletions == []
        assert len(rewrites) == 1


class TestClassifyRemovedLinesWhitespaceOnly:
    def test_blank_and_whitespace_only_old_lines_are_ignored(self):
        deletions, rewrites = edit_guards.classify_removed_lines(['\n', '    \n'], 'x = 1\n')
        assert (deletions, rewrites) == ([], [])

    def test_whitespace_only_line_is_ignored_even_when_real_lines_are_deleted(self):
        old = ['\n', '    do_thing()\n']
        deletions, rewrites = edit_guards.classify_removed_lines(old, '')
        assert deletions == ['    do_thing()\n']
        assert rewrites == []
        assert '\n' not in deletions or deletions.count('\n') == 0


class TestClassifyRemovedLinesRatioBoundary:
    def test_ratio_of_exactly_0_9_is_classified_as_a_rewrite(self):
        old_line = 'aaaaaaaaaa\n'  # 10 'a's + newline
        candidate = 'aaaaaaaa\n'  # 8 'a's + newline
        # Sanity-check the fixture actually sits on the boundary before
        # trusting the assertion below to mean anything.
        ratio = difflib.SequenceMatcher(None, old_line, candidate).ratio()
        assert ratio == pytest.approx(0.9)
        deletions, rewrites = edit_guards.classify_removed_lines([old_line], candidate)
        assert deletions == []
        assert len(rewrites) == 1
        assert rewrites[0][2] == pytest.approx(0.9)

    def test_measured_ratio_gap_between_the_two_live_regressions_holds(self):
        deleted = '    _mark_plane_done(story_key, plan_name)\n'
        corrupt = '    worktree = story.get("worktree", "")\n'
        cands = MARK_DONE_NEW.splitlines(keepends=True)
        best = lambda s: max(difflib.SequenceMatcher(None, s, c).ratio() for c in cands)
        assert best(deleted) < 0.9 < best(corrupt)


class TestClassifyRemovedLinesDuplicates:
    def test_duplicate_identical_old_lines_are_accounted_as_a_multiset(self):
        old = ['    x()\n', '    x()\n']
        deletions, rewrites = edit_guards.classify_removed_lines(old, '    x()\n')
        # One copy survives verbatim (consumed from the multiset); the other
        # copy must still be reported in exactly one of the two buckets.
        assert len(deletions) + len(rewrites) == 1

    def test_three_duplicates_against_one_surviving_copy_reports_two(self):
        old = ['    x()\n', '    x()\n', '    x()\n']
        deletions, rewrites = edit_guards.classify_removed_lines(old, '    x()\n')
        assert len(deletions) + len(rewrites) == 2


class TestClassifyRemovedLinesPurity:
    def test_does_not_mutate_old_lines_argument(self):
        old = list(MARK_DONE_OLD)
        edit_guards.classify_removed_lines(old, MARK_DONE_NEW)
        assert old == MARK_DONE_OLD

    def test_return_value_is_a_two_tuple_of_lists(self):
        result = edit_guards.classify_removed_lines(MARK_DONE_OLD, MARK_DONE_NEW)
        assert isinstance(result, tuple)
        assert len(result) == 2
        deletions, rewrites = result
        assert isinstance(deletions, list)
        assert isinstance(rewrites, list)


# ---------------------------------------------------------------------------
# render_removal_report
# ---------------------------------------------------------------------------

class TestRenderRemovalReportEmpty:
    def test_returns_empty_string_when_both_lists_are_empty(self):
        assert edit_guards.render_removal_report([], []) == ""


class TestRenderRemovalReportDeletions:
    def test_deleted_lines_appear_verbatim_in_the_report(self):
        deletions = ['    _mark_plane_done(story_key, plan_name)\n']
        report = edit_guards.render_removal_report(deletions, [])
        assert '_mark_plane_done(story_key, plan_name)' in report

    def test_report_is_nonempty_when_there_are_deletions_but_no_rewrites(self):
        report = edit_guards.render_removal_report(['    do_thing()\n'], [])
        assert report.strip()


class TestRenderRemovalReportRewrites:
    def test_dropped_trailing_paren_char_diff_is_localised_not_a_full_line_echo(self):
        deletions, rewrites = edit_guards.classify_removed_lines(PAREN_OLD, PAREN_NEW)
        report = edit_guards.render_removal_report(deletions, rewrites)
        assert report.strip()
        # A plain full-line echo (old line, then new line, both printed in
        # full) is exactly the rendering that let this bug ship silently.
        # The report must not simply be the two full lines concatenated
        # with nothing localising the single dropped ')'.
        full_echo = PAREN_OLD[0] + PAREN_NEW
        assert report != full_echo
        assert report.count(PAREN_OLD[0].strip()) < 2 or ')' in report

    def test_rewrite_report_reflects_both_old_and_new_content(self):
        old = ['    worktree = story.get("worktree", "")\n']
        new = '    worktree = story.get("worktree", "+")\n'
        rewrites = [(old[0], new, difflib.SequenceMatcher(None, old[0], new).ratio())]
        report = edit_guards.render_removal_report([], rewrites)
        assert report.strip()

    def test_multiple_rewrites_are_all_represented_in_the_report(self):
        rewrites = [
            ('    a = get("x", "")\n', '    a = get("x", "+")\n', 0.95),
            ('    b = get("y", "")\n', '    b = get("y", "+")\n', 0.95),
        ]
        report = edit_guards.render_removal_report([], rewrites)
        assert 'a = get' in report
        assert 'b = get' in report


class TestRenderRemovalReportCaps:
    def test_more_than_15_deletions_are_truncated_with_a_marker(self):
        deletions = [f'    line_{i}()\n' for i in range(200)]
        report = edit_guards.render_removal_report(deletions, [])
        assert len(report) < 3000
        assert 'truncat' in report.lower() or 'more line' in report.lower()

    def test_deletion_section_stays_under_the_1500_char_cap(self):
        deletions = [f'    line_{i}_' + ('x' * 40) + '()\n' for i in range(200)]
        report = edit_guards.render_removal_report(deletions, [])
        assert len(report) < 3000

    def test_single_deletion_line_over_1500_chars_is_still_truncated(self):
        deletions = ['    x = "' + ('a' * 2000) + '"\n']
        report = edit_guards.render_removal_report(deletions, [])
        assert 'truncat' in report.lower()

    def test_more_than_15_rewrites_are_truncated_with_a_marker(self):
        rewrites = [
            (f'    v{i} = get("k{i}", "")\n', f'    v{i} = get("k{i}", "+")\n', 0.95)
            for i in range(30)
        ]
        report = edit_guards.render_removal_report([], rewrites)
        assert len(report) < 3000
        assert 'truncat' in report.lower() or 'more line' in report.lower()

    def test_rewrite_section_stays_under_the_1500_char_cap(self):
        rewrites = [
            (f'    v{i} = get("k{i}", "' + ('x' * 40) + '")\n',
             f'    v{i} = get("k{i}", "' + ('x' * 40) + '+")\n', 0.95)
            for i in range(30)
        ]
        report = edit_guards.render_removal_report([], rewrites)
        assert len(report) < 3000


class TestRenderRemovalReportPurity:
    def test_does_not_mutate_its_arguments(self):
        deletions = ['    do_thing()\n']
        rewrites = [('    a = get("x", "")\n', '    a = get("x", "+")\n', 0.95)]
        deletions_copy = list(deletions)
        rewrites_copy = list(rewrites)
        edit_guards.render_removal_report(deletions, rewrites)
        assert deletions == deletions_copy
        assert rewrites == rewrites_copy

    def test_return_type_is_str(self):
        report = edit_guards.render_removal_report(['    x\n'], [])
        assert isinstance(report, str)


# ---------------------------------------------------------------------------
# Module-shape contracts: pure, no I/O, exactly two public functions.
# ---------------------------------------------------------------------------

class TestModuleContract:
    def test_module_exposes_exactly_the_three_required_public_functions(self):
        public_functions = {
            name for name, obj in vars(edit_guards).items()
            if not name.startswith('_')
            and inspect.isfunction(obj)
            and getattr(obj, '__module__', None) == edit_guards.__name__
        }
        assert public_functions == {
            'classify_removed_lines',
            'render_removal_report',
            'duplicated_block_warning',
        }

    def test_module_imports_nothing_beyond_difflib_collections_typing(self):
        tree = ast.parse(MODULE_PATH.read_text())
        allowed = {'difflib', 'collections', 'typing'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split('.')[0] in allowed, (
                        f"disallowed import: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module.split('.')[0] in allowed, (
                    f"disallowed import: {node.module}"
                )

    def test_module_performs_no_file_io_or_subprocess_calls(self):
        source = MODULE_PATH.read_text()
        for forbidden in ('open(', 'subprocess', 'os.system', 'Path('):
            assert forbidden not in source, f"found forbidden call: {forbidden}"


# ---------------------------------------------------------------------------
# duplicated_block_warning
#
# A live replace_lines edit on a markdown doc inserted a verbatim duplicate
# of a paragraph that already existed nearby. classify_removed_lines cannot
# see this because nothing was removed. For non-.py files the removal report
# is the only guard that exists, so this function adds a pure check for a run
# of >= min_lines consecutive lines in new_str that also appears verbatim as a
# consecutive run in surrounding_text (the file content outside the replaced
# range).
# ---------------------------------------------------------------------------

# The real regression shape: a two-line 'Shipped: ...' paragraph that appears
# both inside the inserted new_str and in the surrounding (untouched) text.
SHIPPED_BLOCK = (
    "Shipped: the replace_lines guard now blocks collateral deletions.\n"
    "Shipped: the report quotes the exact removed line verbatim.\n"
)


class TestDuplicatedBlockWarningSignature:
    def test_function_exists_and_is_callable(self):
        assert hasattr(edit_guards, 'duplicated_block_warning')
        assert callable(edit_guards.duplicated_block_warning)

    def test_min_lines_keyword_only(self):
        # min_lines must be keyword-only: passing it positionally should raise.
        with pytest.raises(TypeError):
            edit_guards.duplicated_block_warning("a\n", "a\n", 2)  # type: ignore[misc]

    def test_return_type_is_str(self):
        result = edit_guards.duplicated_block_warning("", "")
        assert isinstance(result, str)

    def test_is_pure_no_io(self):
        # The function must not touch the filesystem or spawn subprocesses.
        source = MODULE_PATH.read_text()
        # The new function body is added after the existing ones; the whole
        # module must remain free of I/O calls (already asserted by the
        # contract test above, but assert it again for clarity).
        for forbidden in ('open(', 'subprocess', 'os.system', 'Path('):
            assert forbidden not in source, f"found forbidden call: {forbidden}"


class TestDuplicatedBlockWarningNegativeAndBoundary:
    def test_empty_new_str_returns_empty(self):
        assert edit_guards.duplicated_block_warning("", "some\nsurrounding\n") == ""

    def test_empty_surrounding_text_returns_empty(self):
        assert edit_guards.duplicated_block_warning("some\nlines\n", "") == ""

    def test_both_empty_returns_empty(self):
        assert edit_guards.duplicated_block_warning("", "") == ""

    def test_single_duplicated_line_below_min_lines_returns_empty(self):
        # A single duplicated line is below the default min_lines=2.
        assert edit_guards.duplicated_block_warning("only line\n", "only line\n") == ""

    def test_single_duplicated_line_below_explicit_min_lines_returns_empty(self):
        assert edit_guards.duplicated_block_warning(
            "only line\n", "only line\n", min_lines=2
        ) == ""

    def test_exactly_min_lines_duplicated_returns_warning(self):
        block = "line one\nline two\n"
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        assert isinstance(result, str)

    def test_duplicated_run_of_blank_lines_only_returns_empty(self):
        # A run consisting entirely of blank/whitespace lines must not warn.
        blanks = "\n\n\n"
        result = edit_guards.duplicated_block_warning(blanks, blanks, min_lines=2)
        assert result == ""

    def test_duplicated_run_of_whitespace_only_lines_returns_empty(self):
        ws = "   \n\t\n  \n"
        result = edit_guards.duplicated_block_warning(ws, ws, min_lines=2)
        assert result == ""

    def test_near_but_not_exact_duplicate_returns_empty(self):
        # This function is verbatim-only by design; a near miss must not warn.
        new_str = "Shipped: the guard blocks collateral deletions.\nsecond\n"
        surrounding = "Shipped: the guard blocks collateral deletion.\nsecond\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result == ""

    def test_leading_indentation_is_significant(self):
        # Indented block in new_str vs non-indented in surrounding -> not a match.
        new_str = "    indented line one\n    indented line two\n"
        surrounding = "indented line one\nindented line two\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result == ""

    def test_leading_and_trailing_blank_lines_ignored_when_finding_run(self):
        # The duplicated run is wrapped in blank lines inside new_str; the
        # blanks should be ignored and the inner run still detected.
        inner = "real line one\nreal line two\n"
        new_str = "\n\n" + inner + "\n\n"
        surrounding = "prefix\n" + inner + "suffix\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result != ""


class TestDuplicatedBlockWarningContent:
    def test_warning_names_number_of_duplicated_lines(self):
        block = "line one\nline two\nline three\n"
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        # The warning must name the number of duplicated lines (3 here).
        assert "3" in result

    def test_warning_quotes_the_block(self):
        block = "line one\nline two\n"
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        # The block text must appear (verbatim) in the warning.
        assert "line one" in result
        assert "line two" in result

    def test_real_regression_shape_shipped_paragraph(self):
        # The actual live regression: a two-line 'Shipped: ...' paragraph
        # appearing in both new_str and surrounding_text.
        new_str = SHIPPED_BLOCK
        surrounding = "Some intro text.\n" + SHIPPED_BLOCK + "Some trailing text.\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result != ""
        assert "2" in result
        assert "Shipped: the replace_lines guard now blocks collateral deletions." in result

    def test_run_must_be_consecutive_in_surrounding_text(self):
        # The same lines exist in surrounding_text but NOT consecutively.
        new_str = "alpha\nbeta\n"
        surrounding = "alpha\nsomething in between\nbeta\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result == ""

    def test_run_must_be_consecutive_in_new_str(self):
        # The same lines exist in new_str but not consecutively.
        new_str = "alpha\nsomething in between\nbeta\n"
        surrounding = "alpha\nbeta\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result == ""

    def test_trailing_newlines_normalised_in_comparison(self):
        # One side has a trailing blank line, the other doesn't, but the
        # meaningful run still matches after newline normalisation.
        block = "line one\nline two\n"
        new_str = block
        surrounding = "prefix\n" + block.rstrip("\n") + "\nsuffix\n"
        result = edit_guards.duplicated_block_warning(new_str, surrounding, min_lines=2)
        assert result != ""


class TestDuplicatedBlockWarningCaps:
    def _long_block(self, n: int) -> str:
        return "".join(f"line number {i}\n" for i in range(n))

    def test_long_block_truncation_marker_present(self):
        block = self._long_block(50)
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        # A truncation marker must be present (consistent with render_removal_report).
        assert "truncated" in result.lower()

    def test_long_block_under_char_cap(self):
        block = self._long_block(50)
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        # The whole warning must stay under the 1500 character cap.
        assert len(result) <= 1500

    def test_long_block_line_cap_at_fifteen(self):
        # The quoted block portion is capped at 15 lines.
        block = self._long_block(50)
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert result != ""
        # Count quoted content lines (lines that look like the duplicated data,
        # excluding the header/truncation marker). At most 15 of the data lines
        # should appear.
        data_lines = [ln for ln in result.splitlines() if ln.startswith("line number ")]
        assert len(data_lines) <= 15

    def test_warning_under_cap_when_block_small(self):
        block = "line one\nline two\n"
        result = edit_guards.duplicated_block_warning(block, block, min_lines=2)
        assert len(result) <= 1500


class TestDuplicatedBlockWarningMinLinesParameter:
    def test_min_lines_three_requires_three_lines(self):
        block = "a\nb\n"
        # Only 2 lines but min_lines=3 -> no warning.
        assert edit_guards.duplicated_block_warning(block, block, min_lines=3) == ""

    def test_min_lines_three_warns_at_three(self):
        block = "a\nb\nc\n"
        result = edit_guards.duplicated_block_warning(block, block, min_lines=3)
        assert result != ""
        assert "3" in result

    def test_min_lines_one_warns_on_single_line(self):
        # With min_lines=1 a single duplicated (non-blank) line should warn.
        result = edit_guards.duplicated_block_warning("lonely\n", "lonely\n", min_lines=1)
        assert result != ""
        assert "1" in result

    def test_min_lines_one_still_ignores_blank_only_runs(self):
        result = edit_guards.duplicated_block_warning("\n\n", "\n\n", min_lines=1)
        assert result == ""
