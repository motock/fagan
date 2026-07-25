"""Tests for the CI-fail rework feedback helper `_ci_rework_feedback`.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest test_ci_rework_feedback.py -v

The helper is a pure module-level function on `pipeline.server` (imported as
`p`). It builds the `review_feedback` string the merge-gate CI-fail rework
path hands back to the implementer, branching on whether the failing check
looks like a LINT job (run the linter) or a test/unidentified job (balanced
both-sides: the bug could be in the implementation OR a test file you wrote).
A NEW COMMIT is always required.

These tests call `p._ci_rework_feedback(...)` directly. They are RED until a
later dispatch adds the helper to `pipeline/server.py`.
"""

from pipeline import server as p


# The exact commit-required closing sentence the helper must ALWAYS append.
_COMMIT_REQUIRED = (
    "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed "
    "commits, and exiting without committing a change cannot alter the CI "
    "result."
)


def test_lint_failure_includes_gate_error_verbatim_and_lint_instruction():
    gate_error = "ci fail: Lint (ruff): failure"
    msg = p._ci_rework_feedback(gate_error)

    # 1. gate_error appears verbatim (same first-line failure statement).
    assert gate_error in msg
    # 2. A lint instruction is present (mentions running lint / `ruff check`).
    assert "ruff check" in msg.lower() or "lint" in msg.lower()
    # 3. The old single-hypothesis "incorrect assertion" wording is GONE.
    assert "incorrect assertion" not in msg.lower()
    # 4. The commit-required closing sentence is present.
    assert _COMMIT_REQUIRED in msg


def test_test_failure_uses_balanced_both_sides_wording_no_lint_instruction():
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error)

    # gate_error appears verbatim.
    assert gate_error in msg
    # Balanced both-sides: mentions BOTH the implementation and a test file.
    assert "implementation" in msg.lower()
    assert "test file" in msg.lower()
    # No lint instruction on the test branch.
    assert "ruff check" not in msg.lower()
    assert "lint command" not in msg.lower()
    # Does NOT presume the test is at fault (the old bug).
    assert "incorrect assertion" not in msg.lower()
    # Commit-required sentence present.
    assert _COMMIT_REQUIRED in msg


def test_empty_detail_falls_back_to_balanced_message_never_crashes():
    gate_error = "ci fail: "
    msg = p._ci_rework_feedback(gate_error)

    # Never crashes; returns a string.
    assert isinstance(msg, str)
    assert msg
    # gate_error appears verbatim even when empty-ish.
    assert gate_error in msg
    # Falls back to the balanced both-sides wording (mentions implementation
    # and test file), NOT the lint branch.
    assert "implementation" in msg.lower()
    assert "test file" in msg.lower()
    assert "ruff check" not in msg.lower()
    # Commit-required sentence present.
    assert _COMMIT_REQUIRED in msg


def test_case_insensitive_lint_match_uppercase_LINT():
    gate_error = "ci fail: LINT: failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    # Took the lint branch.
    assert "ruff check" in msg.lower() or "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_case_insensitive_lint_match_eslint():
    gate_error = "ci fail: eslint: failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    # Took the lint branch (eslint is a lint-ish check name).
    assert "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_clippy_takes_lint_branch():
    gate_error = "ci fail: clippy: failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    assert "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_golangci_takes_lint_branch():
    gate_error = "ci fail: golangci-lint: failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    assert "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_unidentified_check_falls_back_to_balanced():
    # A check name that is neither lint-ish nor obviously a test still takes
    # the balanced both-sides branch (defensive default).
    gate_error = "ci fail: Build: failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    assert "implementation" in msg.lower()
    assert "test file" in msg.lower()
    assert "ruff check" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_realistic_semicolon_joined_error_string_with_lint_check():
    # The dependency story's _ci_status produces semicolon-joined
    # "name: conclusion" pairs, truncated to 300 chars. A multi-check string
    # where one check is lint-named must take the lint branch.
    gate_error = "Lint (ruff): failure; Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error)

    assert gate_error in msg
    assert "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_first_line_is_the_failure_statement_with_gate_error():
    # The helper ALWAYS starts with the failure statement including gate_error
    # verbatim - same first line as today's static template.
    gate_error = "ci fail: Lint (ruff): failure"
    msg = p._ci_rework_feedback(gate_error)

    # The failure statement (the line containing gate_error) must be the
    # opening of the message.
    first_line = msg.splitlines()[0]
    assert gate_error in first_line