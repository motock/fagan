r"""Unit tests for app.dashboard._parse_progress's "never raises" contract.

The function's own docstring promises it "fail open — never raises". A regex
with unbounded digit groups (`(\d+)/(\d+)`) violated that contract: an oversized
digit run exceeds CPython's integer-string conversion limit (~4300 digits) and
makes `int(m.group(1))` raise ValueError, which surfaces as a 500 from the
/api/plans/{plan}/stories/{story}/checklist endpoint.

The preferred minimal fix is to bound the digit groups in the regex itself,
e.g. `r'^PROGRESS:\s*(\d{1,6})/(\d{1,6})\s*$'`, so an oversized run simply
fails to match and falls through to the existing `return None` at the end of
the function — no new code path needed.

These tests grade BOTH the mechanism (the regex bound) and the resulting
behavior (never raises / fail open), plus the happy path and boundary values
around the chosen six-digit bound.
"""
import inspect

from app import dashboard as d

# --- Source inspection: the fix must be the bounded regex, not a try/except ---

DASHBOARD_SRC = inspect.getsource(d._parse_progress)


def test_regex_digit_groups_are_bounded_in_source():
    """The PROGRESS regex must bound each digit group so an unbounded run can
    never reach int(). Six digits is the documented bound."""
    assert r'(\d{1,6})/(\d{1,6})' in DASHBOARD_SRC, (
        "expected the bounded regex r'(\\d{1,6})/(\\d{1,6})' in _parse_progress; "
        "the unbounded form is still present or the bound differs"
    )


def test_unbounded_regex_is_gone_from_source():
    r"""The old unbounded `(\d+)/(\d+)` form must be removed (it is the root
    cause). Its continued presence would still allow the ValueError path."""
    assert r'(\d+)/(\d+)' not in DASHBOARD_SRC, (
        "the unbounded regex r'(\\d+)/(\\d+)' is still present in _parse_progress"
    )


def test_parse_progress_is_still_a_single_function_and_anchored():
    """Sanity: the function still exists and the PROGRESS anchor is intact so
    the bound is on the right line, not a different regex."""
    assert callable(d._parse_progress)
    assert 'PROGRESS:' in DASHBOARD_SRC


# --- Happy path: normal progress still parses ---

def test_normal_progress_parses():
    plan = "1. write tests\n2. implement\n3. refactor\n"
    scratch = "PROGRESS: 1/3\n"
    result = d._parse_progress(plan, scratch)
    assert result == {"done": 1, "total": 3}


def test_single_digit_boundary_matches():
    """One digit (the minimum of the {1,6} bound) must still match."""
    plan = "1. step\n"
    scratch = "PROGRESS: 1/1\n"
    assert d._parse_progress(plan, scratch) == {"done": 1, "total": 1}


def test_six_digit_boundary_matches():
    """Six digits (the maximum of the {1,6} bound) must still match and parse
    to the correct integer — this is the largest count the bound admits."""
    plan = "1. step\n"
    scratch = "PROGRESS: 123456/654321\n"
    assert d._parse_progress(plan, scratch) == {"done": 123456, "total": 1}


def test_leading_whitespace_and_trailing_whitespace_tolerated():
    """The \\s* anchors around the digits must still be honored."""
    plan = "1. step\n"
    scratch = "PROGRESS:   2/5   \n"
    assert d._parse_progress(plan, scratch) == {"done": 2, "total": 1}


# --- Negative / boundary: oversized runs must fail open to None, never raise ---

def test_seven_digit_done_fails_open_to_none():
    """Seven digits exceeds the six-digit bound, so the line must NOT match and
    the function returns None (fail open) rather than parsing a 7-digit int."""
    plan = "1. step\n"
    scratch = "PROGRESS: 1234567/2\n"
    assert d._parse_progress(plan, scratch) is None


def test_seven_digit_total_fails_open_to_none():
    """The bound applies to BOTH groups; an oversized total must also fail open."""
    plan = "1. step\n"
    scratch = "PROGRESS: 2/1234567\n"
    assert d._parse_progress(plan, scratch) is None


def test_oversized_digit_run_does_not_raise():
    """The headline regression: a 5000-digit run must not raise ValueError out
    of _parse_progress. It must return None."""
    plan = "1. step\n"
    scratch = f"PROGRESS: {'9' * 5000}/2\n"
    # Must not raise; must fail open.
    result = d._parse_progress(plan, scratch)
    assert result is None


def test_oversized_digit_run_on_total_does_not_raise():
    """Symmetric: an oversized total must not raise either."""
    plan = "1. step\n"
    scratch = f"PROGRESS: 2/{'9' * 5000}\n"
    result = d._parse_progress(plan, scratch)
    assert result is None


# --- Other fail-open cases the contract already promises ---

def test_none_plan_returns_none():
    assert d._parse_progress(None, "PROGRESS: 1/2\n") is None


def test_none_scratchpad_returns_none():
    assert d._parse_progress("1. step\n", None) is None


def test_empty_plan_returns_none():
    assert d._parse_progress("", "PROGRESS: 1/2\n") is None


def test_empty_scratchpad_returns_none():
    assert d._parse_progress("1. step\n", "") is None


def test_plan_with_no_numbered_items_returns_none():
    """total == 0 short-circuits to None before the scratchpad is even read."""
    plan = "just prose, no numbered list\n"
    scratch = "PROGRESS: 1/2\n"
    assert d._parse_progress(plan, scratch) is None


def test_scratchpad_without_progress_line_returns_none():
    plan = "1. step\n"
    scratch = "done: step 1\nnext: step 2\n"
    assert d._parse_progress(plan, scratch) is None


def test_malformed_progress_line_returns_none():
    """Garbage after PROGRESS: must not match and must fail open."""
    plan = "1. step\n"
    scratch = "PROGRESS: abc/def\n"
    assert d._parse_progress(plan, scratch) is None


def test_progress_line_with_extra_text_does_not_match():
    """The $ anchor must still reject trailing non-whitespace."""
    plan = "1. step\n"
    scratch = "PROGRESS: 1/2 extra\n"
    assert d._parse_progress(plan, scratch) is None


def test_first_matching_progress_line_wins():
    """If multiple PROGRESS: lines exist, the first parseable one wins (existing
    behavior must be preserved by the bound)."""
    plan = "1. step\n2. step\n"
    scratch = "PROGRESS: 1/2\nPROGRESS: 2/2\n"
    assert d._parse_progress(plan, scratch) == {"done": 1, "total": 2}


def test_oversized_line_followed_by_valid_line_still_fails_open_for_that_line():
    """A 7-digit line does not match, so the loop continues. If a later line is
    valid, it is used; if not, None. Here only the oversized line exists."""
    plan = "1. step\n"
    scratch = "PROGRESS: 1234567/2\n"
    assert d._parse_progress(plan, scratch) is None


# --- The function must genuinely never raise for any plausible input ---

def test_never_raises_on_huge_all_nines_both_groups():
    """Stress the never-raise contract on both groups simultaneously."""
    plan = "1. step\n"
    scratch = f"PROGRESS: {'9' * 10000}/{('9' * 10000)}\n"
    assert d._parse_progress(plan, scratch) is None