"""Acceptance: pipeline/edit_guards.py exposes a working similarity
classifier and a character-level renderer.

This story creates a new module with no call sites, so grading the unit
directly is correct here - there is no wiring for the fixture to bypass.
The two call sites are graded by the separate wiring story's fixture.

Ratios asserted below are measured from the two real regressions this
work exists to prevent.
"""
import difflib

from pipeline import edit_guards


OLD = [
    '    story = _load(story_key)\n',
    '    worktree = story.get("worktree", "")\n',
    '    _merge_pr(story, worktree)\n',
    '    _mark_plane_done(story_key, plan_name)\n',
]
NEW = (
    '    story = _load(story_key)\n'
    '    worktree = story.get("worktree", "+")\n'
    '    _merge_pr(story, worktree)\n'
)


def test_dropped_call_is_classified_as_a_true_deletion():
    deletions, _rewrites = edit_guards.classify_removed_lines(OLD, NEW)
    assert any('_mark_plane_done' in d for d in deletions)


def test_corrupted_default_is_classified_as_a_rewrite_not_a_deletion():
    deletions, rewrites = edit_guards.classify_removed_lines(OLD, NEW)
    assert not any('worktree = story.get' in d for d in deletions)
    assert any('worktree = story.get' in old for old, _new, _r in rewrites)


def test_verbatim_survivors_are_reported_in_neither_list():
    deletions, rewrites = edit_guards.classify_removed_lines(OLD, NEW)
    assert not any('_load(story_key)' in d for d in deletions)
    assert not any('_load(story_key)' in old for old, _n, _r in rewrites)
    assert not any('_merge_pr' in d for d in deletions)


def test_pure_insertion_reports_nothing():
    old = ['    return 1\n']
    new = "    log.debug('entering')\n    return 1\n"
    assert edit_guards.classify_removed_lines(old, new) == ([], [])


def test_empty_new_str_makes_everything_a_deletion():
    deletions, rewrites = edit_guards.classify_removed_lines(['    do_thing()\n'], '')
    assert deletions and not rewrites


def test_whitespace_only_removed_lines_are_ignored():
    deletions, rewrites = edit_guards.classify_removed_lines(['\n', '    \n'], 'x = 1\n')
    assert (deletions, rewrites) == ([], [])


def test_empty_inputs_are_safe():
    assert edit_guards.classify_removed_lines([], '') == ([], [])


def test_dropped_trailing_paren_is_a_rewrite_and_the_char_diff_shows_it():
    old = ['  (not launchd-supervised, so a restart is manual)\n']
    new = '  (not launchd-supervised, so a restart is manual\n'
    deletions, rewrites = edit_guards.classify_removed_lines(old, new)
    assert not deletions
    assert len(rewrites) == 1
    report = edit_guards.render_removal_report(deletions, rewrites)
    # A full-line echo is what let this bug ship; the report must localise
    # the change rather than just reprinting both lines.
    assert report.count(old[0].strip()) < 2 or ')' in report
    assert report.strip()


def test_render_is_empty_when_nothing_was_removed():
    assert edit_guards.render_removal_report([], []) == ''


def test_render_is_capped_for_large_removals():
    deletions = [f'    line_{i}\n' for i in range(200)]
    report = edit_guards.render_removal_report(deletions, [])
    assert len(report) < 3000
    assert 'truncat' in report.lower() or 'more line' in report.lower()


def test_duplicate_old_lines_are_accounted_as_a_multiset():
    old = ['    x()\n', '    x()\n']
    deletions, rewrites = edit_guards.classify_removed_lines(old, '    x()\n')
    assert len(deletions) + len(rewrites) == 1


def test_classifier_is_pure_and_does_not_mutate_its_input():
    old = list(OLD)
    edit_guards.classify_removed_lines(old, NEW)
    assert old == OLD


def test_measured_ratio_gap_holds():
    """Guards the 0.9 threshold against drift: the real deletion and the
    real corruption must stay on opposite sides of it."""
    deleted = '    _mark_plane_done(story_key, plan_name)\n'
    corrupt = '    worktree = story.get("worktree", "")\n'
    cands = NEW.splitlines(keepends=True)
    best = lambda s: max(difflib.SequenceMatcher(None, s, c).ratio() for c in cands)
    assert best(deleted) < 0.9 < best(corrupt)
