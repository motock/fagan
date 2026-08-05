"""Tests for pipeline.parsers._extract_suggested_commit_message.

This function finds a backtick-quoted string inside reviewer output that is
shaped like a Conventional Commit header, so ``review_story`` can auto-amend
the HEAD commit message when the reviewer's ONLY Blocking finding is about
commit-message format (not file content) - the 2026-08-05 config-surface-a2-cleanup
incident (story eb9ac838, PR #238) where a local model parked purely over
commit-message hygiene even though the reviewer's feedback already contained
a ready-to-use suggested replacement message in backticks.

The shape it must match: an identifier from this repo's commit-type vocabulary
(feat, fix, chore, refactor, test, docs, ci, perf, style, build), optionally
followed by ``(scope)`` and/or ``!``, then ``:``, then at least one
non-whitespace character, all on one line, all inside a single pair of
backticks. It returns the FIRST such match found in the text (without the
backticks), or None if no backtick span matches the shape. It does NOT validate
the subject text further (length, casing, trailing period) - matching the
type/scope/colon shape is enough.

These tests are written to fail until the implementation exists. They mirror
the structure, style, and one-test-per-case granularity of
``test_parsers_blocking_finding_extraction.py`` (read-only reference).
"""
from pipeline import parsers

# The exact text from the real incident's review_feedback field.
_INCIDENT_TEXT = (
    "The HEAD commit message must be rewritten to a Conventional Commits "
    "message such as `style(backend): collapse trailing whitespace and "
    "rewrite incoherent max-steps comment`, or the two commits squashed "
    "into a single proper Conventional Commit."
)
_INCIDENT_EXPECTED = (
    "style(backend): collapse trailing whitespace and "
    "rewrite incoherent max-steps comment"
)


def test_incident_example_type_scope_subject_is_extracted():
    # The literal text from the real incident's review_feedback field.
    assert parsers._extract_suggested_commit_message(_INCIDENT_TEXT) == _INCIDENT_EXPECTED


def test_type_scope_subject_match_is_extracted_without_backticks():
    text = "Rewrite to `fix(auth): handle expired tokens` please."
    assert parsers._extract_suggested_commit_message(text) == "fix(auth): handle expired tokens"


def test_type_only_no_scope_matches():
    text = "Use `docs: update README` instead."
    assert parsers._extract_suggested_commit_message(text) == "docs: update README"


def test_breaking_change_bang_matches():
    text = "Suggested: `feat(api)!: drop v1 endpoints`."
    assert parsers._extract_suggested_commit_message(text) == "feat(api)!: drop v1 endpoints"


def test_breaking_change_bang_without_scope_matches():
    text = "Suggested: `refactor!: rewrite the module`."
    assert parsers._extract_suggested_commit_message(text) == "refactor!: rewrite the module"


def test_all_recognized_types_match():
    for t in ("feat", "fix", "chore", "refactor", "test", "docs", "ci", "perf", "style", "build"):
        text = f"`{t}: do something`"
        assert parsers._extract_suggested_commit_message(text) == f"{t}: do something", t


def test_text_with_no_backticks_returns_none():
    text = "The HEAD commit message must be a Conventional Commit like fix: thing."
    assert parsers._extract_suggested_commit_message(text) is None


def test_backtick_span_not_starting_with_recognized_type_returns_none():
    text = "Look at `some random code` for reference."
    assert parsers._extract_suggested_commit_message(text) is None


def test_empty_string_returns_none():
    assert parsers._extract_suggested_commit_message("") is None


def test_backtick_span_missing_colon_returns_none():
    text = "Use `fix auth handle expired tokens` instead."
    assert parsers._extract_suggested_commit_message(text) is None


def test_backtick_span_missing_subject_after_colon_returns_none():
    # A type/scope with a colon but nothing after it is not a valid header.
    text = "Use `fix(auth):` instead."
    assert parsers._extract_suggested_commit_message(text) is None


def test_second_backtick_span_is_the_valid_one_is_still_found():
    # The first backtick span is NOT a Conventional Commit shape; the second
    # one is. The function must find the shaped one, not blindly grab the
    # first backticked span.
    text = (
        "See `some random code` for context, but rewrite to "
        "`chore(deps): bump requests` please."
    )
    assert parsers._extract_suggested_commit_message(text) == "chore(deps): bump requests"


def test_first_valid_match_is_returned_when_multiple_valid_spans():
    text = "Use `fix(a): one` or `fix(b): two`."
    assert parsers._extract_suggested_commit_message(text) == "fix(a): one"


def test_unrecognized_type_returns_none():
    text = "Use `wip(x): checkpoint-1` instead."
    assert parsers._extract_suggested_commit_message(text) is None


def test_multiline_backtick_span_does_not_match():
    # The match must be on a single line; a backtick span whose content spans
    # lines is not a Conventional Commit header.
    text = "Use `fix(a):\n do something` instead."
    assert parsers._extract_suggested_commit_message(text) is None


def test_exported_in_all():
    # The function must be exported in pipeline.parsers.__all__, placed right
    # next to _extract_blocking_finding_files and _has_review_findings.
    assert "_extract_suggested_commit_message" in parsers.__all__


def test_exported_alongside_siblings_in_all():
    # The task requires the name be added right next to _extract_blocking_finding_files
    # and _has_review_findings in __all__. Assert all three are present.
    names = parsers.__all__
    assert "_extract_blocking_finding_files" in names
    assert "_has_review_findings" in names
    assert "_extract_suggested_commit_message" in names