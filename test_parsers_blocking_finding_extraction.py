"""Tests for pipeline.parsers._extract_blocking_finding_files.

This function extracts the file path from every Blocking finding line in
reviewer output, per the updated agents/code-reviewer.md 'Output contract'
which requires every Blocking finding to be written on its own line as

    - Blocking: <relative/file/path>: <one-line description>

(Suggestion/Nit findings are unaffected and have no format requirement.)

The extraction is best-effort: reviewer output that doesn't follow the new
format simply yields no tracked targets (an empty list), never an error.
"""
import pipeline.parsers as parsers


def test_single_well_formed_blocking_line_extracts_path():
    text = "VERDICT: REQUEST_CHANGES\n- Blocking: path/to/file.py: missing error handling"
    assert parsers._extract_blocking_finding_files(text) == ["path/to/file.py"]


def test_multiple_blocking_lines_across_files_preserve_order():
    text = (
        "VERDICT: REQUEST_CHANGES\n"
        "- Blocking: alpha/a.py: needs a guard\n"
        "- Blocking: beta/b.py: off-by-one\n"
        "- Blocking: gamma/c.py: leak"
    )
    assert parsers._extract_blocking_finding_files(text) == [
        "alpha/a.py",
        "beta/b.py",
        "gamma/c.py",
    ]


def test_suggestion_and_nit_lines_are_ignored_even_with_file_path():
    text = (
        "VERDICT: REQUEST_CHANGES\n"
        "- Suggestion: path/to/file.py: could rename\n"
        "- Nit: other/file.py: trailing whitespace"
    )
    assert parsers._extract_blocking_finding_files(text) == []


def test_duplicate_blocking_lines_on_same_file_deduplicate():
    text = (
        "VERDICT: REQUEST_CHANGES\n"
        "- Blocking: dup.py: first issue\n"
        "- Blocking: dup.py: second issue\n"
        "- Blocking: other.py: third issue"
    )
    assert parsers._extract_blocking_finding_files(text) == ["dup.py", "other.py"]


def test_verdict_line_with_no_blocking_lines_returns_empty():
    text = "VERDICT: REQUEST_CHANGES\n- Suggestion: foo.py: minor"
    assert parsers._extract_blocking_finding_files(text) == []


def test_empty_string_input_returns_empty():
    assert parsers._extract_blocking_finding_files("") == []


def test_blocking_line_missing_path_prefix_is_not_matched():
    # Free prose without the required "path:" form must not crash and yields
    # no tracked target for that line.
    text = "VERDICT: REQUEST_CHANGES\n- Blocking: this finding has no file path"
    assert parsers._extract_blocking_finding_files(text) == []


def test_blocking_label_is_case_insensitive():
    text = "VERDICT: REQUEST_CHANGES\n- blocking: lower/case.py: issue"
    assert parsers._extract_blocking_finding_files(text) == ["lower/case.py"]


def test_blocking_line_without_leading_dash_matches():
    # The regex tolerates an optional leading dash / whitespace.
    text = "VERDICT: REQUEST_CHANGES\nBlocking: nodash.py: issue"
    assert parsers._extract_blocking_finding_files(text) == ["nodash.py"]


def test_exported_in_all():
    # The function must be importable from parsers.__all__ alongside the
    # existing _parse_verdict / _has_review_findings exports.
    assert "_extract_blocking_finding_files" in parsers.__all__