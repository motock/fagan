"""Tests for pipeline.parsers._synthesize_test_failure_feedback.

Mode 40 follow-up: when a story reaches review_story via the
acceptance_failed_review opt-in (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1) with
a known-failing last_test_check, sending the submission to the LLM reviewer
wastes a call whose principal finding is just the failing-test list the
gate already recorded - observed live on MODE40-LOCAL-LINT-GATE (16 failing
tests, and the review's own "Test suite results" section just restated
what last_test_check already had). This synthesizes REQUEST_CHANGES
feedback directly, in the same format _parse_verdict/
_extract_blocking_finding_files already expect, so downstream handling
(rework routing, Mode 24/28's finding-target tracking) is unaffected.
"""
from pipeline.parsers import (
    _extract_blocking_finding_files,
    _has_review_findings,
    _parse_verdict,
    _synthesize_test_failure_feedback,
)


def test_verdict_is_request_changes():
    text = _synthesize_test_failure_feedback({
        "cmd": ["pytest", "-q"], "returncode": 1,
        "stdout_tail": "FAILED test_foo.py::test_bar - assert False\n",
        "stderr_tail": "",
    })
    assert _parse_verdict(text) == "REQUEST_CHANGES"


def test_has_findings_beyond_bare_verdict():
    text = _synthesize_test_failure_feedback({
        "cmd": ["pytest", "-q"], "returncode": 1,
        "stdout_tail": "FAILED test_foo.py::test_bar - assert False\n",
        "stderr_tail": "",
    })
    assert _has_review_findings(text)


def test_extracts_blocking_finding_file_from_pytest_failed_lines():
    text = _synthesize_test_failure_feedback({
        "cmd": ["pytest", "-q"], "returncode": 1,
        "stdout_tail": (
            "FAILED test_foo.py::test_bar - assert False\n"
            "FAILED test_foo.py::test_baz - assert True\n"
            "FAILED test_other.py::test_qux - KeyError\n"
        ),
        "stderr_tail": "",
    })
    files = _extract_blocking_finding_files(text)
    assert files == ["test_foo.py", "test_other.py"]  # dedup, order preserved


def test_no_parseable_failed_lines_still_emits_one_blocking_line():
    """A non-pytest runner (or truncated tail) whose FAILED lines don't
    match the pytest shape must still produce at least one Blocking line -
    otherwise Mode 24/28's finding-target tracking sees zero findings and
    can't guard a later re-approval."""
    text = _synthesize_test_failure_feedback({
        "cmd": ["cargo", "test"], "returncode": 1,
        "stdout_tail": "error[E0308]: mismatched types\n",
        "stderr_tail": "",
    })
    assert _extract_blocking_finding_files(text) == []
    assert "- Blocking:" in text
    assert _parse_verdict(text) == "REQUEST_CHANGES"


def test_includes_the_actual_failing_command_and_tail():
    text = _synthesize_test_failure_feedback({
        "cmd": ["/venv/bin/python", "-m", "pytest", "-q"], "returncode": 1,
        "stdout_tail": "FAILED test_x.py::test_y - assert 1 == 2\n",
        "stderr_tail": "",
    })
    assert "/venv/bin/python -m pytest -q" in text
    assert "assert 1 == 2" in text


def test_combines_stdout_and_stderr_tails():
    text = _synthesize_test_failure_feedback({
        "cmd": ["pytest"], "returncode": 1,
        "stdout_tail": "FAILED test_a.py::test_b\n",
        "stderr_tail": "Traceback (most recent call last):\n",
    })
    assert "FAILED test_a.py::test_b" in text
    assert "Traceback" in text


def test_handles_missing_tail_fields_gracefully():
    """A minimal last_test_check dict (no stdout_tail/stderr_tail keys at
    all) must not raise - defensive against callers passing a partial dict."""
    text = _synthesize_test_failure_feedback({"cmd": ["pytest"], "returncode": 1})
    assert _parse_verdict(text) == "REQUEST_CHANGES"
