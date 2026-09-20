"""Tests for the oracle-variant local dispatch agent loop (scripts/local_agent_oracle.py): 5xx escalation, transcript persistence/resume, and the finish_if_green oracle done-bar.

Split out of test_local_agent_oracle.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `lao` module itself) moved to tests.unit._local_agent_oracle_test_helpers.
"""
import ast
import inspect

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _finish_if_green_spy,
    _init_git_repo,
    _isolate_environ,
    _sequence_chat,
    lao,
)


def test_finish_if_green_suite_rejections_reset_on_green(monkeypatch):
    """A run that recovers (suite goes green) must not carry stale rejection
    credit into a later rework round - the counter resets on success so the
    cap only ever fires on CONSECUTIVE unsatisfiable rounds."""
    messages, _commits, _full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="1 failed"
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "REWORK_SUITE_REJECT_CAP", 3)
    lao._reset_suite_rejections()

    assert lao.finish_if_green(1, messages=messages) is False
    assert lao.finish_if_green(2, messages=messages) is False

    # Now the suite goes green: finish_if_green terminates and clears the count.
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (True, "", None))
    assert lao.finish_if_green(3, messages=messages) is True
    assert lao.suite_reject_cap_reached() is False


def test_full_suite_result_runs_unscoped_test_cmd_and_captures_tail(monkeypatch, tmp_path):
    """_full_suite_result runs the detected test_cmd UNscoped - the full
    worktree suite, NOT acceptance-scoped like oracle_result. It must reuse
    detect_test_command + the heavy lock and return (rc==0, tail[-500:]),
    mirroring the merge gate's _ci_status_stub (tests/benchmark/harness.py)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_oracle.py"])

    recorded: dict = {}

    def _detect(cwd):
        recorded["cwd"] = cwd
        return (tmp_path, ["pytest", "-q"])

    class _R:
        returncode = 1
        stdout = ""
        stderr = "FAILED test_rate_limiter.py::test_time_backwards_no_refill - assert 9.0 == 3.0"

    def _run(argv, cwd, capture_output, text):
        recorded["argv"] = argv
        return _R()

    monkeypatch.setattr(lao.p, "detect_test_command", _detect)
    monkeypatch.setattr(lao.p, "_is_heavy", lambda argv: False)
    monkeypatch.setattr(lao.subprocess, "run", _run)

    ok, tail, gate = lao._full_suite_result()
    assert ok is False
    assert gate == "test"
    # Unscoped: the acceptance paths were NOT appended (contrast oracle_result,
    # which appends ACCEPTANCE_PATHS to the pytest argv).
    assert recorded["argv"] == ["pytest", "-q"]
    assert "assert 9.0 == 3.0" in tail
    assert len(tail) <= 500


def test_full_suite_result_no_test_cmd_returns_pass(monkeypatch, tmp_path):
    """No detectable test command -> nothing to fail. Return (True, '') so the
    rework done-bar is satisfied (mirrors _ci_status_stub's 'no test_cmd ->
    pass' and oracle_result's 'no acceptance -> pass')."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_oracle.py"])
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, None))
    ok, tail, gate = lao._full_suite_result()
    assert ok is True
    assert tail == ""
    assert gate is None


# ---------------------------------------------------------------------------
# L1 done-bypass gap (found in live validation 2026-07-18): the oracle agent
# has TWO termination paths - the auto finish_if_green (gated above) AND a
# model-called `done` tool. The `done` handler must ALSO require the full
# suite green on a rework round, or the model can dodge the raised done-bar by
# calling `done` (observed: gpt-oss called done on round 3 with its own pasted
# pytest showing "3 failed, 19 passed" - oracle green, done accepted, bypassed
# the gate finish_if_green enforces).
# ---------------------------------------------------------------------------

def test_oracle_done_rejected_on_rework_round_when_full_suite_fails(
    tmp_path, monkeypatch, capsys
):
    """CI-fail-rework round: model calls `done`, oracle green, but the agent's
    own test still fails. The `done` handler must NOT terminate - it must feed
    the failing excerpt back and reject, mirroring finish_if_green's gate, so
    the model can't dodge the raised done-bar by calling done instead of
    letting the auto-check fire. Bounded by MAX_STEPS."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "MAX_STEPS", 3)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    excerpt = "FAILED test_rate_limiter.py::test_time_moves_backward - assert True is False"
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (False, excerpt, "test"))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)

    fake, calls = _sequence_chat([("done", {"summary": "all done"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc != 0, f"done bypassed the rework full-suite gate; rc={rc}\n{out!r}"
    assert "DONE (oracle green)" not in out, out
    assert "done rejected — full test suite still fails" in out, out
    # The excerpt was fed back to the next turn.
    assert len(calls) >= 2
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]
    assert excerpt in last_user["content"], last_user["content"]


def test_oracle_done_rejected_on_lint_gate_when_tests_pass(
    tmp_path, monkeypatch, capsys
):
    """Sibling of the test-gate case above: oracle green, tests PASS, but the
    lint gate fails. The `done` handler must reject with the LINT-gate message
    (tell the agent to run `ruff check . --fix`, NOT to edit implementation
    logic), mirroring finish_if_green's gate-aware branch. This is the exact
    W1c-08 incident shape (tests green, ruff red) ported to the oracle done
    bypass path."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "MAX_STEPS", 3)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    lint_excerpt = "test_foo.py:5:1: F401 'os' imported but unused"
    monkeypatch.setattr(lao, "_full_suite_result",
                        lambda: (False, lint_excerpt, "lint"))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)

    fake, calls = _sequence_chat([("done", {"summary": "all done"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc != 0, f"done bypassed the lint gate; rc={rc}\n{out!r}"
    assert "DONE (oracle green)" not in out, out
    # Gate-aware: the lint-gate print fires, NOT the test-gate print.
    assert "done rejected — lint gate still fails" in out, out
    assert "full test suite still fails" not in out, out
    # The lint excerpt was fed back, with the lint-specific guidance.
    assert len(calls) >= 2
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]
    assert lint_excerpt in last_user["content"], last_user["content"]
    assert "ruff check . --fix" in last_user["content"], last_user["content"]
    assert "Do NOT edit implementation logic" in last_user["content"], last_user["content"]


def test_oracle_done_rejected_message_does_not_presume_the_test_is_wrong(
    tmp_path, monkeypatch,
):
    """Ported alongside test_done_rejected_message_does_not_presume_the_test_is_wrong
    (local_agent.py) - keep both copies in sync. Once Gap 1 armed this gate
    for ordinary REVIEW rework (not just CI-fail rework), the flat assertion
    that the agent's OWN test is wrong stopped being reliably true - the
    failure can equally be a still-incomplete implementation."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "MAX_STEPS", 3)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    excerpt = "FAILED test_review_story_lock_guard.py::test_review_story_skips_when_lock_held"
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (False, excerpt, "test"))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)

    fake, calls = _sequence_chat([("done", {"summary": "all done"})])
    monkeypatch.setattr(lao, "chat", fake)

    lao.main()
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]["content"]

    assert "your own committed test has a wrong assertion" not in last_user
    assert "implementation" in last_user.lower()
    assert excerpt in last_user


def test_oracle_done_accepted_on_cold_start_without_consulting_suite(
    tmp_path, monkeypatch, capsys
):
    """Cold start (REWORK_FULL_SUITE unset): `done` with oracle green is
    accepted as today (rc=0) and the full suite is NEVER consulted - proves
    the done-handler gate is scoped to rework rounds, not global."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    suite_calls: list = []
    monkeypatch.setattr(
        lao, "_full_suite_result",
        lambda: suite_calls.append(True) or (False, "would-fail-but-uncalled", "test"),
    )
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)

    fake, _ = _sequence_chat([("done", {"summary": "done"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    assert rc == 0  # accepted, as today
    assert suite_calls == []  # suite never consulted on a cold start


def test_oracle_done_accepted_on_rework_round_when_full_suite_green(
    tmp_path, monkeypatch
):
    """CI-fail-rework round, agent fixed its own test: oracle green AND full
    suite green -> `done` accepted (rc=0). The convergence case through the
    done path."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (True, "", None))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)

    fake, _ = _sequence_chat([("done", {"summary": "fixed"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    assert rc == 0


# ---------------------------------------------------------------------------
# Mode 40 follow-up: kept in sync with test_local_agent.py's equivalent
# block - lint feedback both per-edit (fast, in-run) and as part of the
# done-bar's _full_suite_result.
# ---------------------------------------------------------------------------

def test_full_suite_result_runs_lint_after_tests_pass_and_fails_on_lint_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(lao.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(lao.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        if argv[0] == "pytest":
            return _R(0)
        return _R(1, err="test_foo.py:5:1: F401 'os' imported but unused")

    monkeypatch.setattr(lao.subprocess, "run", _run)

    ok, tail, gate = lao._full_suite_result()
    assert ok is False
    assert gate == "lint"
    assert "F401" in tail
    assert calls == [["pytest", "-q"], ["ruff", "check", "."]]


def test_full_suite_result_tests_and_lint_both_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(lao.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(lao.p, "_is_heavy", lambda argv: False)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: _R())

    ok, tail, gate = lao._full_suite_result()
    assert ok is True
    assert tail == ""
    assert gate is None


def test_full_suite_result_skips_lint_when_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(lao.p, "detect_lint_command", lambda cwd: None)
    monkeypatch.setattr(lao.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _run)

    ok, _tail, gate = lao._full_suite_result()
    assert ok is True
    assert gate is None
    assert calls == [["pytest", "-q"]]


def test_full_suite_result_does_not_run_lint_when_tests_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(lao.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(lao.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err="FAILED test_x.py::test_y")

    monkeypatch.setattr(lao.subprocess, "run", _run)

    ok, tail, gate = lao._full_suite_result()
    assert ok is False
    assert gate == "test"
    assert "test_y" in tail
    # Re-run once before rejecting (retry-once exemption).
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


def test_create_file_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: F401 'os' imported but unused\n"
        stderr = ""

    calls = []

    def _run(argv, cwd, capture_output, text, timeout=None):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _run)

    result = lao.run_tool("create_file", {"path": "foo.py", "content": "import os\n"})
    assert "created foo.py" in result
    assert "F401" in result
    assert calls == [["ruff", "check", "foo.py"]]


def test_str_replace_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "foo.py").write_text("x = 1\n")
    monkeypatch.setattr(lao.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: E741 ambiguous variable name 'l'\n"
        stderr = ""

    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: _R())

    result = lao.run_tool("str_replace", {"path": "foo.py", "old_str": "x = 1\n", "new_str": "l = 1\n"})
    assert "edited foo.py" in result
    assert "E741" in result


def test_replace_lines_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "foo.py").write_text("x = 1\n")
    monkeypatch.setattr(lao.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: E741 ambiguous variable name 'l'\n"
        stderr = ""

    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: _R())

    # Replacing "x = 1" with "l = 1" is a true deletion (no near-survivor),
    # so the edit-guards gate rejects it unless confirm_removals opts in.
    # This test grades the lint-feedback suffix, not the gate, so opt in.
    result = lao.run_tool("replace_lines", {"path": "foo.py", "start": 1, "end": 1, "new_str": "l = 1\n", "confirm_removals": True})
    assert "edited foo.py" in result
    assert "E741" in result


def test_lint_feedback_silent_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: _R())

    result = lao.run_tool("create_file", {"path": "clean.py", "content": "x = 1\n"})
    assert result == "created clean.py"


def test_lint_feedback_skipped_for_non_python_files(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))
    calls = []
    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: calls.append(1))

    result = lao.run_tool("create_file", {"path": "notes.md", "content": "hello\n"})
    assert result == "created notes.md"
    assert calls == []


def test_lint_feedback_skipped_when_lint_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao.p, "detect_lint_command", lambda cwd: None)
    calls = []
    monkeypatch.setattr(lao.subprocess, "run", lambda *a, **k: calls.append(1))

    result = lao.run_tool("create_file", {"path": "foo.py", "content": "x = 1\n"})
    assert result == "created foo.py"
    assert calls == []


# ---------------------------------------------------------------------------
# Mode 41 follow-up (D): kept in sync with test_local_agent.py's equivalent
# block - replace_lines echoes lines it genuinely removed.
# ---------------------------------------------------------------------------

def test_replace_lines_no_echo_when_old_line_preserved_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = "def f():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)

    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 2,
        "new_str": "    log.debug('entering')\n    return 1\n",
    })

    assert result.startswith("edited mod.py (lines 2-2)")
    assert "removed" not in result.lower()


def test_replace_lines_echo_capped_for_large_removals(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = "def f():\n" + "".join(f"    line_{i}\n" for i in range(200)) + "    return\n"
    (tmp_path / "mod.py").write_text(original)

    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 201,
        "new_str": "    pass\n",
    })

    # 200 genuine deletions (line_0..line_199 -> "    pass\n") are now a hard
    # block, not a capped echo: the edit must be refused and the file left
    # untouched, with the removal report still capped (not a 200-line dump).
    assert "The edit was NOT applied." in result
    assert "deletes 200 line(s)" in result
    assert (tmp_path / "mod.py").read_text() == original
    assert len(result) < 3000
    assert "truncated" in result.lower()


def test_str_replace_does_not_echo_removed_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("x = 1\n")
    result = lao.run_tool("str_replace", {"path": "mod.py", "old_str": "x = 1\n", "new_str": "x = 2\n"})
    assert result == "edited mod.py"
    assert "removed" not in result.lower()


# ---------------------------------------------------------------------------
# S2 edit-guards wiring (mirrors the equivalent block added to
# test_local_agent.py - see that file for the fuller rationale). These are
# NEW tests, not modifications of the block above: the oracle's pre-existing
# test_replace_lines_echoes_removed_lines_in_result /
# test_replace_lines_echo_capped_for_large_removals /
# test_replace_lines_appends_lint_feedback_when_ruff_finds_issues are left
# untouched here, out of this dispatch's authorization - see the dispatch
# summary for the resulting conflicts those pre-existing tests now have with
# the wired gate.
# ---------------------------------------------------------------------------

def test_oracle_replace_lines_rejects_true_deletion_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def f():\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)

    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    do_thing()\n",
    })

    assert "time.sleep(10)" in result
    assert "The edit was NOT applied." in result
    assert (tmp_path / "mod.py").read_text() == original


def test_oracle_replace_lines_confirm_removals_true_writes_the_rejected_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def f():\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)

    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    do_thing()\n",
        "confirm_removals": True,
    })

    assert "The edit was NOT applied." not in result
    assert result.startswith("edited mod.py (lines 2-3)")
    written = (tmp_path / "mod.py").read_text()
    assert written != original
    assert "time.sleep(10)" not in written


def test_local_agent_oracle_imports_edit_guards_module():
    from pipeline import edit_guards
    assert lao.edit_guards is edit_guards


def test_oracle_replace_lines_confirm_removals_declared_optional_in_tool_schema():
    entry = next(t for t in lao.TOOLS if t["function"]["name"] == "replace_lines")
    params = entry["function"]["parameters"]
    assert params["properties"]["confirm_removals"]["type"] == "boolean"
    assert "confirm_removals" not in params.get("required", [])


def test_oracle_replace_lines_rejection_error_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = lao.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert "mod.py" in result


def test_oracle_replace_lines_rejection_error_tells_model_how_to_proceed(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = lao.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert "confirm_removals" in result
    assert "new_str" in result


def test_oracle_replace_lines_rejection_error_ends_with_exact_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = lao.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert result.rstrip().endswith("The edit was NOT applied.")


def test_oracle_replace_lines_rejection_error_embeds_the_real_removal_report(tmp_path, monkeypatch):
    from pipeline import edit_guards
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def f():\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)
    old_lines = original.splitlines(keepends=True)
    new_str = "    do_thing()\n"
    deletions, rewrites = edit_guards.classify_removed_lines(old_lines[1:3], new_str)
    expected_report = edit_guards.render_removal_report(deletions, rewrites)

    result = lao.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 3, "new_str": new_str})

    assert expected_report
    assert expected_report in result


def test_oracle_replace_lines_success_message_uses_char_diff_report_for_rewrites(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text('x = cfg.get("k", "")\n')

    result = lao.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1,
        "new_str": 'x = cfg.get("k", "+")\n',
    })

    assert result.startswith("edited mod.py (lines 1-1)")
    assert "The edit was NOT applied." not in result
    assert '- x = cfg.get("k", "")' in result
    assert '+ x = cfg.get("k", "+")' in result
    assert "not present in your replacement" not in result
    assert (tmp_path / "mod.py").read_text() == 'x = cfg.get("k", "+")\n'


def test_oracle_removed_lines_echo_helper_is_deleted():
    assert not hasattr(lao, "_removed_lines_echo")


def test_oracle_counter_import_removed_as_dead_code_but_deque_kept():
    assert not hasattr(lao, "Counter")
    assert hasattr(lao, "deque")


# ---------------------------------------------------------------------------
# Issue 97a29c75 (oracle mirror): wire the unconditional top-level-symbol-loss
# check into replace_lines/str_replace and name lost symbols explicitly.
# Mirrors the local_agent.py tests exactly so the two-file mirror stays in sync.
# ---------------------------------------------------------------------------

def test_oracle_replace_lines_names_dropped_top_level_symbols(tmp_path, monkeypatch):
    """replace_lines whose range fully removes an unreferenced top-level
    function AND constant must name both symbols in the rejection."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def helper_func():\n"
        "    return 1\n"
        "\n"
        "SOME_CONST = 42\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 1,
        "end": 5,
        "new_str": "# unrelated comment\n",
    })
    assert "The edit was NOT applied." in result
    assert "helper_func" in result
    assert "SOME_CONST" in result
    assert "permanently removes these top-level symbols" in result


def test_oracle_str_replace_names_dropped_top_level_symbols(tmp_path, monkeypatch):
    """str_replace whose old_str fully covers an unreferenced top-level
    function AND constant must name both symbols in the rejection."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def helper_func():\n"
        "    return 1\n"
        "\n"
        "SOME_CONST = 42\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    old_str = "def helper_func():\n    return 1\n\nSOME_CONST = 42\n"
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": old_str,
        "new_str": "# unrelated comment\n",
    })
    assert "The edit was NOT applied." in result
    assert "helper_func" in result
    assert "SOME_CONST" in result


def test_oracle_replace_lines_confirm_removals_true_applies_dropped_symbol_edit(tmp_path, monkeypatch):
    """The bypass: the same edit that names dropped symbols must still apply
    when confirm_removals=true is passed (existing bypass behavior preserved)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def helper_func():\n"
        "    return 1\n"
        "\n"
        "SOME_CONST = 42\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 1,
        "end": 5,
        "new_str": "# unrelated comment\n",
        "confirm_removals": True,
    })
    assert "The edit was NOT applied." not in result
    assert result.startswith("edited mod.py")
    written = (tmp_path / "mod.py").read_text()
    assert "helper_func" not in written
    assert "SOME_CONST" not in written
    assert "keeper" in written


def test_oracle_replace_lines_no_full_symbol_removed_keeps_raw_report_only(tmp_path, monkeypatch):
    """Negative (a): removing lines from INSIDE a function body (no complete
    top-level symbol removed) must NOT trigger the new symbol-naming line."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def keeper():\n"
        "    do_thing()\n"
        "    time.sleep(10)\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    do_thing()\n",
    })
    assert "permanently removes these top-level symbols" not in result


def test_oracle_str_replace_rename_names_old_symbol_as_removed(tmp_path, monkeypatch):
    """Negative (b): a str_replace that renames a top-level function
    (old_name -> new_name) must name old_name as removed."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = "def old_name():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "def old_name():\n    return 1\n",
        "new_str": "def new_name():\n    return 1\n",
    })
    assert "The edit was NOT applied." in result
    assert "old_name" in result


def test_oracle_str_replace_blocks_constant_only_top_level_drop_without_confirm(tmp_path, monkeypatch):
    """A str_replace that fully removes a top-level constant assignment
    (no def/class touched) without confirm_removals must be BLOCKED: it
    returns an ERROR string naming the dropped constant, and the file on
    disk is left unchanged (still contains the constant).

    This is the var-only-drop case the dropped_defs-only gate used to let
    through. The gate must now check the combined dropped (defs + vars)
    list, matching create_file's existing overwrite guard.
    """
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "OLD_TIMEOUT = 30\n\n",
        "new_str": "",
    })
    # Blocked: an ERROR, not an applied edit.
    assert result.startswith("ERROR")
    assert "The edit was NOT applied." in result
    # The dropped constant is named in the rejection (from the combined
    # `dropped` list, not the defs-only list).
    assert "OLD_TIMEOUT" in result
    assert "permanently removes these top-level symbols" in result
    # The file on disk is unchanged.
    assert (tmp_path / "mod.py").read_text() == original
    assert "OLD_TIMEOUT" in (tmp_path / "mod.py").read_text()


def test_oracle_str_replace_constant_only_drop_confirm_removals_true_applies(tmp_path, monkeypatch):
    """The bypass: the same constant-only drop that is blocked without the
    flag must still apply when confirm_removals=true is passed (existing
    bypass behavior preserved for the widened gate)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "OLD_TIMEOUT = 30\n\n",
        "new_str": "",
        "confirm_removals": True,
    })
    assert "The edit was NOT applied." not in result
    assert result.startswith("edited mod.py")
    written = (tmp_path / "mod.py").read_text()
    assert "OLD_TIMEOUT" not in written
    assert "keeper" in written


def test_oracle_str_replace_constant_only_drop_names_only_the_constant(tmp_path, monkeypatch):
    """When a constant-only drop is blocked, the rejection must name the
    actual dropped symbol(s) from the combined `dropped` list and must NOT
    silently omit a var-only drop. The surviving def must not be falsely
    reported as removed."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "OLD_TIMEOUT = 30\n\n",
        "new_str": "",
    })
    assert "OLD_TIMEOUT" in result
    # The surviving def is not reported as removed.
    assert "keeper" not in result


# ---------------------------------------------------------------------------
# Regression: the original _full_suite_result (code review, agent/cc446d9f...
# branch, Blocking #3) was left in the file as dead code - its signature was
# edited to claim `-> tuple[bool, str, str | None]` but its body still
# returns 2-tuples (`return True, ""` / `return False, tail`) - and it is
# permanently shadowed by `_full_suite_result = _full_suite_result_new`
# below it. Every runtime call to `lao._full_suite_result(...)` resolves
# through that alias to the correctly-reimplemented `_full_suite_result_new`,
# so calling the public name cannot surface this bug - it must be checked
# statically against the module source, matching how the review found it.
# ---------------------------------------------------------------------------


def test_full_suite_result_return_statements_match_its_3_tuple_annotation():
    """The FunctionDef literally named `_full_suite_result` in the module
    source must be the sole definition of that name, and every tuple-valued
    `return` in its body must have 3 elements, matching its
    `-> tuple[bool, str, str | None]` annotation. Today this finds the
    original (dead, shadowed) definition, whose body still returns 2-tuples
    - a lying signature the review flagged."""
    source = inspect.getsource(lao)
    tree = ast.parse(source)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_full_suite_result"
    ]
    assert len(matches) == 1, (
        f"expected exactly one `def _full_suite_result`, found {len(matches)}"
    )
    func = matches[0]
    tuple_returns = [
        stmt.value
        for stmt in ast.walk(func)
        if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Tuple)
    ]
    assert tuple_returns, "expected at least one tuple-valued return statement"
    for tup in tuple_returns:
        assert len(tup.elts) == 3, (
            f"a `return` statement in _full_suite_result has {len(tup.elts)} "
            "elements but the function is annotated "
            "-> tuple[bool, str, str | None]"
        )


def test_no_orphaned_full_suite_result_new_symbol():
    """_full_suite_result_new must not exist as a leftover duplicate name -
    the review flagged it (plus the `_full_suite_result = _full_suite_result_new`
    alias line) as dead-code debris that should be deleted/renamed away, not
    left shadowing the real `_full_suite_result` name."""
    assert not hasattr(lao, "_full_suite_result_new"), (
        "_full_suite_result_new should be renamed to _full_suite_result, "
        "not left as an orphaned duplicate definition"
    )
