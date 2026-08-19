"""Tests for the `attempts`-aware CI-fail rework feedback helper.

These tests cover the round-escalation + pytest-excerpt-parsing changes to
`pipeline.server._ci_rework_feedback` described in the round-escalation story:

  * a NEW required `attempts: int` parameter;
  * when `attempts >= 2`, a `PREVIOUS REWORK ATTEMPT {n-1} DID NOT FIX THIS.`
    prefix on whichever existing branch (lint vs non-lint) applies;
  * a best-effort `_parse_pytest_excerpt(gate_error) -> str | None` helper
    that extracts a real `file:line` + assertion from `gate_error` and NEVER
    fabricates one (fails closed to `None`);
  * when a parse succeeds AND `attempts >= 2`, the parsed excerpt is appended
    (in addition to the verbatim `Gate error:` line) along with
    `Do not call done until the full suite passes.`;
  * `attempts == 1` (the first rework round) stays byte-identical to today's
    wording -- no prefix, no excerpt addition even when one is parseable.

The existing `tests/unit/test_ci_rework_feedback.py` is left UNTOUCHED. These
tests are RED until a later dispatch implements the changes above.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest \
        tests/unit/test_ci_rework_feedback_attempts.py -v
"""

import inspect

from pipeline import server as p

# The exact commit-required closing sentence the helper ALWAYS appends (copied
# from the existing test module so we assert it survives the change too).
_COMMIT_REQUIRED = (
    "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed "
    "commits, and exiting without committing a change cannot alter the CI "
    "result."
)

# The exact prefix the helper must prepend when attempts >= 2.
def _prev_prefix(attempts: int) -> str:
    return f"PREVIOUS REWORK ATTEMPT {attempts - 1} DID NOT FIX THIS. "


# ---------------------------------------------------------------------------
# Signature / call-site requirements.
# ---------------------------------------------------------------------------


def test_signature_has_required_attempts_int_parameter():
    """`_ci_rework_feedback` must now take a required `attempts: int`."""
    sig = inspect.signature(p._ci_rework_feedback)
    params = sig.parameters
    assert "attempts" in params, (
        "_ci_rework_feedback must accept an `attempts` parameter"
    )
    ap = params["attempts"]
    # It must be REQUIRED (no default) -- the call site always has it in scope.
    assert ap.default is inspect.Parameter.empty, (
        "`attempts` must be a required parameter (no default)"
    )
    assert ap.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    ), "`attempts` must be passable positionally"
    # Annotated as int.
    assert ap.annotation is int, (
        f"`attempts` must be annotated int, got {ap.annotation!r}"
    )


def test_call_site_passes_attempts_positionally():
    """The single production call site must pass `attempts` into the helper.

    We read the source (not run the scheduler) so this is a static, cheap
    check that the implementer wired the already-computed `attempts` through.
    """
    # The call site is NOT inside the helper itself; find it in the module.
    mod_src = inspect.getsource(p)
    # There must be exactly one call to _ci_rework_feedback in the module that
    # passes two arguments (gate_error, attempts).
    import re

    calls = re.findall(r"_ci_rework_feedback\(([^)]*)\)", mod_src)
    # At least one call passes two args.
    two_arg = [c for c in calls if len([a for a in c.split(",") if a.strip()]) >= 2]
    assert any("attempts" in c for c in two_arg), (
        "the production call site must pass `attempts` into _ci_rework_feedback"
    )


def test_parse_pytest_excerpt_helper_exists():
    """The best-effort parser helper must exist as a module attribute."""
    assert hasattr(p, "_parse_pytest_excerpt"), (
        "pipeline.server must define `_parse_pytest_excerpt`"
    )


# ---------------------------------------------------------------------------
# (a) attempts == 1 -> output matches today's exact wording (both branches).
# ---------------------------------------------------------------------------


def test_attempts_1_lint_branch_matches_today_wording():
    """Round 1 lint branch is byte-identical to today (no prefix, no excerpt)."""
    gate_error = "ci fail: Lint (ruff): failure"
    msg = p._ci_rework_feedback(gate_error, 1)

    # No round-escalation prefix on round 1.
    assert "PREVIOUS REWORK ATTEMPT" not in msg
    # Today's lint-branch wording is intact.
    assert gate_error in msg
    assert "ruff check" in msg.lower() or "lint" in msg.lower()
    assert "incorrect assertion" not in msg.lower()
    assert _COMMIT_REQUIRED in msg
    # No excerpt addition on round 1 even though this gate_error has no
    # parseable excerpt anyway -- the point is round 1 never adds one.
    assert "Do not call done until the full suite passes." not in msg


def test_attempts_1_nonlint_branch_matches_today_wording():
    """Round 1 non-lint branch is byte-identical to today."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 1)

    assert "PREVIOUS REWORK ATTEMPT" not in msg
    assert gate_error in msg
    assert "implementation" in msg.lower()
    assert "test file" in msg.lower()
    assert "ruff check" not in msg.lower()
    assert _COMMIT_REQUIRED in msg
    assert "Do not call done until the full suite passes." not in msg


def test_attempts_1_with_parseable_excerpt_still_no_excerpt_addition():
    """Even when gate_error carries a parseable excerpt, round 1 must NOT
    append it -- round 1 stays exactly as it is today."""
    gate_error = (
        "ci fail: Test (Python 3.14): failure\n"
        "tests/unit/test_x.py:42: AssertionError: assert 3 == 9"
    )
    msg = p._ci_rework_feedback(gate_error, 1)

    # Round 1: no prefix, no excerpt line, no done-bar line.
    assert "PREVIOUS REWORK ATTEMPT" not in msg
    assert "Do not call done until the full suite passes." not in msg
    # The verbatim gate_error is still present (today's behavior).
    assert gate_error in msg


# ---------------------------------------------------------------------------
# (b) attempts == 2 -> output starts with the PREVIOUS REWORK ATTEMPT prefix.
# ---------------------------------------------------------------------------


def test_attempts_2_starts_with_previous_rework_prefix_nonlint():
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 2)

    prefix = _prev_prefix(2)
    assert msg.startswith(prefix), (
        f"round-2 feedback must start with {prefix!r}; got {msg[:80]!r}"
    )
    # Existing non-lint wording still present after the prefix.
    assert gate_error in msg
    assert "implementation" in msg.lower()
    assert "test file" in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_attempts_2_starts_with_previous_rework_prefix_lint():
    gate_error = "ci fail: Lint (ruff): failure"
    msg = p._ci_rework_feedback(gate_error, 2)

    prefix = _prev_prefix(2)
    assert msg.startswith(prefix)
    # Existing lint wording still present after the prefix.
    assert gate_error in msg
    assert "ruff check" in msg.lower() or "lint" in msg.lower()
    assert _COMMIT_REQUIRED in msg


def test_attempts_3_prefix_counts_previous_attempt_as_2():
    """The prefix number is attempts-1, so round 3 says attempt 2 failed."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 3)
    assert msg.startswith(_prev_prefix(3))
    assert "PREVIOUS REWORK ATTEMPT 2 DID NOT FIX THIS." in msg


def test_attempts_2_prefix_is_at_the_very_start_before_failure_statement():
    """The prefix must come BEFORE the existing failure statement, not after."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 2)
    first_line = msg.splitlines()[0]
    assert first_line.startswith(_prev_prefix(2))
    # The original failure statement text follows the prefix on the same line.
    assert "The merge-gate CI check failed" in first_line


# ---------------------------------------------------------------------------
# (c) parseable excerpt at attempts >= 2 -> output names file:line explicitly.
# ---------------------------------------------------------------------------


def test_attempts_2_parseable_excerpt_names_file_and_line():
    gate_error = (
        "ci fail: Test (Python 3.14): failure\n"
        "tests/unit/test_x.py:42: AssertionError: assert 3 == 9"
    )
    msg = p._ci_rework_feedback(gate_error, 2)

    # The parsed file:line must appear explicitly in the output.
    assert "test_x.py:42" in msg, (
        "round-2 feedback with a parseable excerpt must name the file:line"
    )
    # The verbatim gate_error line is STILL present (addition, not replacement).
    assert gate_error in msg
    # The done-bar instruction is appended alongside the excerpt.
    assert "Do not call done until the full suite passes." in msg
    # Prefix still present.
    assert msg.startswith(_prev_prefix(2))


def test_attempts_2_parseable_excerpt_includes_assertion_text():
    """The excerpt should surface the assertion, not just the file:line."""
    gate_error = "src/app.py:7: AssertionError: assert 3 == 9"
    msg = p._ci_rework_feedback(gate_error, 2)
    assert "app.py:7" in msg
    # The assertion body is literally present in gate_error, so the excerpt
    # should carry it through (not invent a different one).
    assert "assert 3 == 9" in msg


def test_parse_pytest_excerpt_returns_string_for_file_line_assertion():
    """The parser directly: a clean file:line: AssertionError line parses."""
    gate_error = "tests/unit/test_x.py:42: AssertionError: assert 3 == 9"
    excerpt = p._parse_pytest_excerpt(gate_error)
    assert excerpt is not None
    assert isinstance(excerpt, str)
    assert "test_x.py:42" in excerpt


def test_parse_pytest_excerpt_returns_string_for_failed_nodeid_line():
    """A `FAILED <nodeid>` line followed by an assertion should also parse."""
    gate_error = (
        "FAILED tests/unit/test_x.py::test_thing\n"
        "tests/unit/test_x.py:42: AssertionError: assert 3 == 9"
    )
    excerpt = p._parse_pytest_excerpt(gate_error)
    assert excerpt is not None
    assert "test_x.py:42" in excerpt


# ---------------------------------------------------------------------------
# (d) no parseable excerpt -> fall back to verbatim gate_error, no fabrication.
# ---------------------------------------------------------------------------


def test_parse_pytest_excerpt_returns_none_for_plain_classification():
    """A bare classification with no file:line must return None (fail closed)."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    excerpt = p._parse_pytest_excerpt(gate_error)
    assert excerpt is None


def test_parse_pytest_excerpt_returns_none_for_empty_string():
    assert p._parse_pytest_excerpt("") is None


def test_parse_pytest_excerpt_returns_none_for_garbage():
    assert p._parse_pytest_excerpt("totally unrelated noise with no colon line") is None


def test_attempts_2_no_parseable_excerpt_falls_back_to_verbatim_no_fabrication():
    """When nothing parses, round-2 output keeps verbatim gate_error and
    invents NO file:line anywhere."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 2)

    assert msg.startswith(_prev_prefix(2))
    # Verbatim gate_error present.
    assert gate_error in msg
    # No fabricated file:line -- nothing that looks like `<word>.py:<digits>`.
    import re

    fabricated = re.findall(r"\b[\w/]+\.py:\d+\b", msg)
    # The only permissible file:line substrings are ones literally present in
    # gate_error; here gate_error has none, so the output must have none.
    assert fabricated == [], (
        f"round-2 feedback fabricated file:line(s) {fabricated!r} not in gate_error"
    )
    # No done-bar line when there was no excerpt to append.
    assert "Do not call done until the full suite passes." not in msg


def test_attempts_2_lint_branch_no_parseable_excerpt_no_fabrication():
    gate_error = "ci fail: Lint (ruff): failure"
    msg = p._ci_rework_feedback(gate_error, 2)
    assert msg.startswith(_prev_prefix(2))
    assert gate_error in msg
    import re

    assert re.findall(r"\b[\w/]+\.py:\d+\b", msg) == []
    assert "Do not call done until the full suite passes." not in msg


def test_parser_never_fabricates_file_line_not_in_gate_error():
    """The parser must only return file:line/assertion literally present in
    gate_error. Feed it something with a colon but no real file:line and
    confirm it returns None rather than inventing one."""
    # "foo:bar: baz" has colons but no <digits> line number and no real file.
    excerpt = p._parse_pytest_excerpt("foo:bar: baz qux")
    assert excerpt is None


# ---------------------------------------------------------------------------
# Boundary: attempts == 0 (defensive -- should behave like round 1, no prefix).
# ---------------------------------------------------------------------------


def test_attempts_0_no_prefix_defensive():
    """attempts < 2 must never emit the prefix. 0 is a defensive boundary."""
    gate_error = "ci fail: Test (Python 3.14): failure"
    msg = p._ci_rework_feedback(gate_error, 0)
    assert "PREVIOUS REWORK ATTEMPT" not in msg
    assert gate_error in msg


# ---------------------------------------------------------------------------
# The verbatim Gate error line is preserved even when an excerpt is appended.
# ---------------------------------------------------------------------------


def test_excerpt_is_appended_in_addition_to_gate_error_line():
    """The excerpt is ADDED, not a replacement for the `Gate error:` line."""
    gate_error = (
        "ci fail: Test (Python 3.14): failure\n"
        "tests/unit/test_x.py:42: AssertionError: assert 3 == 9"
    )
    msg = p._ci_rework_feedback(gate_error, 2)
    # The full verbatim gate_error (including its classification prefix) is
    # still in the message.
    assert "ci fail: Test (Python 3.14): failure" in msg
    assert "test_x.py:42" in msg
    assert "Do not call done until the full suite passes." in msg