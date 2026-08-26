"""Tests for the local dispatch agent loop (scripts/local_agent.py): live chars/token calibration from ollama's own prompt_eval_count, and the remaining churn/repetition/off-task integration tests.

Split out of test_local_agent.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `la` module itself) moved to tests.unit._local_agent_test_helpers.
"""


from tests.unit._local_agent_test_helpers import (
    _OFF_TASK_BRIEF,
    _sequence_chat,
    la,
    lag,
)


def test_create_file_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: F401 'os' imported but unused\n"
        stderr = ""

    calls = []

    def _run(argv, cwd, capture_output, text, timeout=None):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(la.subprocess, "run", _run)

    result = la.run_tool("create_file", {"path": "foo.py", "content": "import os\n"})
    assert "created foo.py" in result
    assert "F401" in result
    assert calls == [["ruff", "check", "foo.py"]]  # single-file scoped, not whole-repo


def test_str_replace_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "foo.py").write_text("x = 1\n")
    monkeypatch.setattr(la.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: E741 ambiguous variable name 'l'\n"
        stderr = ""

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: _R())

    result = la.run_tool("str_replace", {"path": "foo.py", "old_str": "x = 1\n", "new_str": "l = 1\n"})
    assert "edited foo.py" in result
    assert "E741" in result


def test_replace_lines_appends_lint_feedback_when_ruff_finds_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "foo.py").write_text("x = 1\n")
    monkeypatch.setattr(la.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 1
        stdout = "foo.py:1:1: E741 ambiguous variable name 'l'\n"
        stderr = ""

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: _R())

    # Replacing "x = 1" with "l = 1" is a true deletion (no near-survivor),
    # so the edit-guards gate rejects it unless confirm_removals opts in.
    # This test grades the lint-feedback suffix, not the gate, so opt in.
    result = la.run_tool("replace_lines", {"path": "foo.py", "start": 1, "end": 1, "new_str": "l = 1\n", "confirm_removals": True})
    assert "edited foo.py" in result
    assert "E741" in result


def test_lint_feedback_silent_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: _R())

    result = la.run_tool("create_file", {"path": "clean.py", "content": "x = 1\n"})
    assert result == "created clean.py"


def test_lint_feedback_skipped_for_non_python_files(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_lint_command",
                        lambda cwd: (tmp_path, ["ruff", "check", "."]))
    calls = []
    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: calls.append(1))

    result = la.run_tool("create_file", {"path": "notes.md", "content": "hello\n"})
    assert result == "created notes.md"
    assert calls == []  # ruff never invoked for a non-.py file


def test_lint_feedback_skipped_when_lint_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: None)
    calls = []
    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: calls.append(1))

    result = la.run_tool("create_file", {"path": "foo.py", "content": "x = 1\n"})
    assert result == "created foo.py"
    assert calls == []


# ---------------------------------------------------------------------------
# Mode 41 follow-up (D) -> S2 edit-guards wiring: replace_lines used to
# merely ECHO the lines it actually removed back in the tool result (a
# message the model was free to ignore). Neither _newly_undefined_names nor
# _newly_undefined_module_defs catches a silently-dropped statement with no
# surviving reference to orphan (e.g. a bare `time.sleep(10)` call, or a
# `story["x"] = x` write nothing else reads) - the exact shape of the live
# MODE40-CI-ERROR-DETAIL-V2/MODE40-LINT-GATE-WIRING regressions (a deleted
# time.sleep(10) causing a busy-loop; a deleted story["interrupted_at"]
# assignment breaking dashboard staleness). S2 upgrades the echo into a hard
# BLOCK, via pipeline.edit_guards.classify_removed_lines/render_removal_report
# (see tests/unit/test_edit_guards.py for the classifier itself - these
# tests grade the wiring: the import, the tool schema, and the run_tool gate).
# ---------------------------------------------------------------------------

def test_replace_lines_rejects_true_deletion_by_default(tmp_path, monkeypatch):
    """Supersedes the old echo-only assertion (test_replace_lines_echoes_
    removed_lines_in_result): this exact scenario - a bare time.sleep(10)
    with no exact-or-near survivor in new_str - is now a hard rejection,
    not just a heads-up echo. The file must be left byte-identical."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def f():\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)

    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    do_thing()\n",
    })

    assert "time.sleep(10)" in result
    assert "The edit was NOT applied." in result
    assert (tmp_path / "mod.py").read_text() == original


def test_replace_lines_confirm_removals_true_writes_the_rejected_edit(tmp_path, monkeypatch):
    """The escape hatch: repeating the identical call with
    confirm_removals=True must let the same edit through and actually
    write the file this time."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def f():\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)

    result = la.run_tool("replace_lines", {
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


def test_local_agent_imports_edit_guards_module():
    """CHANGE 1: run_tool's gate must be backed by the real, already-merged
    pipeline.edit_guards module - not a local reimplementation."""
    from pipeline import edit_guards
    assert la.edit_guards is edit_guards


def test_replace_lines_confirm_removals_declared_optional_in_tool_schema():
    """CHANGE 2: the flag is useless if the model is never told it exists,
    and must stay optional so every pre-existing replace_lines call (which
    never passes it) keeps validating against the schema."""
    entry = next(t for t in la.TOOLS if t["function"]["name"] == "replace_lines")
    params = entry["function"]["parameters"]
    assert params["properties"]["confirm_removals"]["type"] == "boolean"
    assert "confirm_removals" not in params.get("required", [])


def test_replace_lines_rejection_error_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = la.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert "mod.py" in result


def test_replace_lines_rejection_error_tells_model_how_to_proceed(tmp_path, monkeypatch):
    """(c) from the story brief: the ERROR must point the model at both
    escape routes - revise new_str to keep the line, or repeat the
    identical call with confirm_removals=true."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = la.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert "confirm_removals" in result
    assert "new_str" in result


def test_replace_lines_rejection_error_ends_with_exact_sentence(tmp_path, monkeypatch):
    """(d) from the story brief: must match the adjacent orphaned-names
    guard's wording verbatim so the model learns one consistent stop
    signal instead of two slightly different ones."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    time.sleep(10)\n    return\n")

    result = la.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 2, "new_str": ""})

    assert result.rstrip().endswith("The edit was NOT applied.")


def test_replace_lines_rejection_error_embeds_the_real_removal_report(tmp_path, monkeypatch):
    """Grades the INTEGRATION, not a hand-rolled reimplementation: the
    ERROR text must contain the exact string edit_guards.render_removal_
    report produces for this edit's actual deletions/rewrites, proving
    run_tool calls the real classifier rather than approximating its
    output inline."""
    from pipeline import edit_guards
    monkeypatch.setattr(la, "CWD", tmp_path)
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

    result = la.run_tool("replace_lines", {"path": "mod.py", "start": 2, "end": 3, "new_str": new_str})

    assert expected_report
    assert expected_report in result


def test_replace_lines_success_message_uses_char_diff_report_for_rewrites(tmp_path, monkeypatch):
    """A rewrite-only edit (no true deletions) is NOT blocked and still
    writes - but its success suffix must now come from edit_guards.
    render_removal_report's character-level diff markers ('- '/'+ '
    lines), not the old full-line echo phrasing that hid a single
    changed character behind a wall of identical-looking text."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text('x = cfg.get("k", "")\n')

    result = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1,
        "new_str": 'x = cfg.get("k", "+")\n',
    })

    assert result.startswith("edited mod.py (lines 1-1)")
    assert "The edit was NOT applied." not in result
    assert '- x = cfg.get("k", "")' in result
    assert '+ x = cfg.get("k", "+")' in result
    assert "not present in your replacement" not in result  # old echo phrasing is gone
    assert (tmp_path / "mod.py").read_text() == 'x = cfg.get("k", "+")\n'


def test_removed_lines_echo_helper_is_deleted():
    """CHANGE 4: nothing outside its own file called _removed_lines_echo
    (verified via `grep -n _removed_lines_echo -R .` before this story
    shipped - only pipeline/edit_guards.py's docstring and tests/unit/
    test_edit_guards.py's docstring reference it in prose). Once
    replace_lines stops calling it, it must be deleted, not left as dead
    code sitting unused beside its replacement."""
    assert not hasattr(la, "_removed_lines_echo")


def test_counter_import_removed_as_dead_code_but_deque_kept():
    """Counter was imported from collections solely for
    _removed_lines_echo's multiset diff; deleting that function without
    also dropping the now-unused import would fail `ruff check .`
    (F401 unused import). deque is still used elsewhere (recent_tools)
    and must stay."""
    assert not hasattr(la, "Counter")
    assert hasattr(la, "deque")


def test_replace_lines_no_echo_when_old_line_preserved_verbatim(tmp_path, monkeypatch):
    """A pure insertion - the old line reappears verbatim inside new_str,
    alongside newly added lines - has nothing genuinely removed, so no
    echo. Only lines that DON'T survive anywhere in the replacement block
    get flagged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = "def f():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)

    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 2,
        "new_str": "    log.debug('entering')\n    return 1\n",
    })

    assert result.startswith("edited mod.py (lines 2-2)")
    assert "removed" not in result.lower()


def test_replace_lines_echo_capped_for_large_removals(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = "def f():\n" + "".join(f"    line_{i}\n" for i in range(200)) + "    return\n"
    (tmp_path / "mod.py").write_text(original)

    result = la.run_tool("replace_lines", {
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
    """str_replace's old_str IS the removed content, byte-for-byte, as
    already specified by the model in its own tool call - echoing it back
    would be redundant. Only replace_lines (stale-line-number-prone) gets
    the echo."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("x = 1\n")
    result = la.run_tool("str_replace", {"path": "mod.py", "old_str": "x = 1\n", "new_str": "x = 2\n"})
    assert result == "edited mod.py"
    assert "removed" not in result.lower()


# ---------------------------------------------------------------------------
# Mode 31 follow-up (2026-08-07): a live dispatch (W3a story 2) exposed that
# the off-task-drift guard's escalation depended on DISTINCT paths, so a
# model that fixated on the SAME off-task file after the one nudge got no
# further guard action for the rest of the run (9 more mutations, 0 further
# nudges). The same investigation found four more real gaps in the guard
# suite: the failing-str_replace guard had no escalation past its one nudge;
# no guard caught successful-but-non-converging edit churn on ONE file (the
# actual biggest cost in that live incident — 22 edits, 0 test runs); `bash`
# could mutate a file directly (ruff --fix, sed -i) invisible to every
# path-based guard; and two guards' "nudged" flags never re-armed after real
# progress, so a run that legitimately recovered would over-eagerly park on
# its next (unrelated) trip instead of getting a fresh nudge. This section
# covers all five fixes.
# ---------------------------------------------------------------------------

def test_off_task_step_first_flag_nudges():
    action, nudged = la._off_task_step("x.py", {"y.py"}, set(), False)
    assert action == "nudge"
    assert nudged is True


def test_off_task_step_on_task_path_is_none_and_leaves_nudged_unchanged():
    action, nudged = la._off_task_step("y.py", {"y.py"}, set(), False)
    assert action == "none"
    assert nudged is False


def test_off_task_step_second_flag_on_the_same_path_escalates():
    """The Mode 31 follow-up bug precisely: a SECOND mutation of the SAME
    already-nudged path must escalate, not silently pass through."""
    targets = {"x.py"}
    action, nudged = la._off_task_step("x.py", {"y.py"}, targets, True)
    assert action == "escalate"
    assert nudged is True


def test_off_task_step_second_flag_on_a_different_path_still_escalates():
    """Unchanged from before the fix — a second DISTINCT off-task path after
    the nudge must still escalate."""
    targets = {"x.py"}
    action, nudged = la._off_task_step("z.py", {"y.py"}, targets, True)
    assert action == "escalate"
    assert nudged is True


def test_off_task_same_path_repeated_mutation_parks_after_nudge(tmp_path, monkeypatch, capsys):
    """The exact live failure: create_file on an off-task path nudges once,
    a SECOND mutation of that SAME path must now park (rc=3) — previously
    it passed through with no further guard action at all."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("str_replace", {
            "path": "totally/unrelated/scratch.py",
            "old_str": "x = 1", "new_str": "x = 2",
        }),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, out
    assert "[parking: off-task drift onto totally/unrelated/scratch.py after nudge]" in out, out
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"


def test_off_task_park_disabled_renudges_on_repeated_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("str_replace", {
            "path": "totally/unrelated/scratch.py",
            "old_str": "x = 1", "new_str": "x = 2",
        }),
        ("done", {"summary": "done"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[off-task nudge:") == 1, out
    assert "[parking: off-task drift onto totally/unrelated/scratch.py after nudge]" in out, (
        f"detection must still fire, output: {out!r}"
    )
    assert len(calls) > 2, f"disabled parking should let the run continue, got {len(calls)}"


def test_bash_off_task_path_detects_ruff_fix():
    """The live incident's exact bypass: `ruff check <off-task file> --fix`
    mutates the file through bash, invisible to the file-tool-only check."""
    expected = {"pipeline/server.py"}
    assert la._bash_off_task_path(
        "ruff check pipeline/env_var_catalog.py --fix", expected
    ) == "pipeline/env_var_catalog.py"


def test_bash_off_task_path_ignores_non_mutating_commands():
    expected = {"pipeline/server.py"}
    assert la._bash_off_task_path(
        "pytest tests/unit/test_env_var_catalog.py", expected
    ) is None


def test_bash_off_task_path_ignores_on_task_paths():
    expected = {"pipeline/server.py"}
    assert la._bash_off_task_path("black pipeline/server.py", expected) is None


def test_bash_off_task_path_ignores_commands_with_no_mutating_marker():
    expected = {"pipeline/server.py"}
    assert la._bash_off_task_path("cat pipeline/env_var_catalog.py", expected) is None


def test_bash_ruff_fix_on_off_task_path_nudges(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    (tmp_path / "totally").mkdir()
    (tmp_path / "totally" / "unrelated.py").write_text("x=1\n")
    responses = [
        ("bash", {"command": "ruff check totally/unrelated.py --fix"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge: totally/unrelated.py not in assigned scope]" in out, out
    assert rc == 0, f"a single off-task bash mutation must not park; got rc={rc}\n{out!r}"


def test_str_replace_fail_escalates_to_park_after_continued_failures(tmp_path, monkeypatch, capsys):
    """The failing-str_replace guard previously nudged once and then did
    nothing else for the rest of the run, no matter how many more times the
    model kept retrying str_replace on the same path. It must now escalate
    to a park after STR_REPLACE_FAIL_ESCALATE_AFTER more failures."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n")
    responses = [
        ("str_replace", {"path": "f.py", "old_str": "NOPE", "new_str": "y"})
        for _ in range(6)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[str_replace-fail nudge:") == 1, out
    assert "[parking: str_replace still failing on f.py after nudge]" in out, out
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) == 4, f"expected to park at the 4th failure, took {len(calls)} calls"


def test_str_replace_fail_park_disabled_renudges_instead_of_terminating(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    (tmp_path / "f.py").write_text("x = 1\n")
    responses = [
        ("str_replace", {"path": "f.py", "old_str": "NOPE", "new_str": "y"})
        for _ in range(5)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[str_replace-fail nudge:") == 1, out
    assert "[parking: str_replace still failing on f.py after nudge]" in out, out
    assert len(calls) > 4, f"disabled parking should let the run continue, got {len(calls)}"


def test_churn_step_resets_on_path_change():
    state = {"path": "", "count": 0, "nudged": False}
    assert la._churn_step("a.py", state) == "none"
    assert la._churn_step("a.py", state) == "none"
    assert state["path"] == "a.py"
    assert state["count"] == 2
    assert la._churn_step("b.py", state) == "none"
    assert state["path"] == "b.py"
    assert state["count"] == 1


def test_churn_guard_nudges_then_parks_on_same_path_edits_with_no_test_run(
    tmp_path, monkeypatch, capsys
):
    """The biggest single cost in the live incident: 22 consecutive
    SUCCESSFUL edits to one ON-task file, zero test runs, and no existing
    guard ever fired (each success resets every other guard's state). This
    is a new guard - not present before this fix at all."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(lag, "CHURN_SAME_PATH_MAX_EDITS", 3)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        ("str_replace", {"path": "pow.rs", "old_str": "// stub", "new_str": "// stub 1"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 1", "new_str": "// stub 2"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 2", "new_str": "// stub 3"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 3", "new_str": "// stub 4"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 4", "new_str": "// stub 5"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 5", "new_str": "// stub 6"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[churn nudge:") == 1, out
    assert "[parking: churn on pow.rs continues with no test run]" in out, out
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) == 6, f"expected to park at the 6th edit, took {len(calls)} calls"


def test_churn_guard_does_not_fire_when_pytest_runs_between_edits(tmp_path, monkeypatch, capsys):
    """A model that DOES verify its work between edits (runs pytest) must
    not be treated as blind churn — the streak resets on a pytest call."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(lag, "CHURN_SAME_PATH_MAX_EDITS", 3)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        ("str_replace", {"path": "pow.rs", "old_str": "// stub", "new_str": "// stub 1"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 1", "new_str": "// stub 2"}),
        ("bash", {"command": "pytest -q"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 2", "new_str": "// stub 3"}),
        ("str_replace", {"path": "pow.rs", "old_str": "// stub 3", "new_str": "// stub 4"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[churn nudge:" not in out, out
    assert rc == 0, f"expected clean finish, got {rc}\noutput: {out!r}"


def test_repetition_guard_rearms_after_a_successful_mutation(tmp_path, monkeypatch, capsys):
    """Before this fix, `nudged_repeat` never reset, so a run that tripped
    the guard once, then made genuine progress, then LATER hit a fresh,
    unrelated repeat pattern would skip straight to parking on that later
    trip instead of getting the same one-nudge-first treatment every other
    first-time trip gets. A real edit invalidates the staleness this guard
    reacts to (the underlying `seen` counter is already reset on success);
    the "nudged" flag must be too."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        ("view_file", {"path": "a.txt"}),
        ("view_file", {"path": "a.txt"}),
        ("view_file", {"path": "a.txt"}),  # 1st trip -> nudge
        ("create_file", {"path": "new.py", "content": "x = 1\n"}),  # real progress
        ("view_file", {"path": "b.txt"}),
        ("view_file", {"path": "b.txt"}),
        ("view_file", {"path": "b.txt"}),  # unrelated repeat -> should nudge again
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[repetition nudge]") == 2, out
    assert "[parking: repeated action after nudge]" not in out, out
    assert rc == 0, f"expected clean finish, got {rc}\noutput: {out!r}"


def test_read_heavy_guard_rearms_after_a_successful_mutation(tmp_path, monkeypatch, capsys):
    """Same class of fix as the repetition-guard re-arm, for `nudged_read_heavy`
    / `distinct_windows`. Two separate 6-distinct-read windows, with a real
    edit in between, must each get their own fresh nudge — not have the
    second window silently treated as post-nudge exploration."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = (
        [("bash", {"command": f"cat {c}"}) for c in "abcdef"]
        + [("create_file", {"path": "new.py", "content": "x = 1\n"})]
        + [("bash", {"command": f"cat {c}"}) for c in "ghijkl"]
        + [("done", {"summary": "done"})]
    )
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[read-heavy nudge:") == 2, out
    assert "[parking: read-heavy" not in out, out
    assert rc == 0, f"expected clean finish, got {rc}\noutput: {out!r}"


def test_repetition_guard_answers_all_orphaned_calls_in_a_multi_call_turn(
    tmp_path, monkeypatch, capsys
):
    """A single assistant turn that bundles multiple tool_calls, where a
    guard trips mid-batch (break), must not leave any LATER tool_calls entry
    from that same turn unanswered — Mode 33 traced exactly this shape to
    gpt-oss:20b's Harmony-format output degrading a few turns later. Only
    reproducible with a custom multi-call fake, since this harness's real
    models issue one tool call per turn in practice."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "x.txt").write_text("hi\n")
    turn = {"n": 0}
    calls: list = []

    def fake_chat(messages):
        calls.append(messages)
        turn["n"] += 1
        if turn["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "view_file", "arguments": {"path": "x.txt"}}},
                {"function": {"name": "view_file", "arguments": {"path": "x.txt"}}},
                {"function": {"name": "view_file", "arguments": {"path": "x.txt"}}},
                {"function": {"name": "bash", "arguments": {"command": "echo late"}}},
            ]}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "done", "arguments": {"summary": "done"}}}]}

    monkeypatch.setattr(la, "chat", fake_chat)

    la.main()
    capsys.readouterr()

    messages = calls[-1]
    for i, m in enumerate(messages[:-1]):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = messages[i + 1]
            assert nxt.get("role") == "tool", (
                f"assistant tool_calls at index {i} has no tool-role answer "
                f"(orphaned tool call); next message is {nxt!r}"
            )
    orphan_stubs = [
        m for m in messages
        if m.get("role") == "tool" and "skipped" in (m.get("content") or "")
    ]
    assert len(orphan_stubs) == 1, (
        f"expected exactly 1 stub answer for the orphaned 4th call, "
        f"got {len(orphan_stubs)}: {messages!r}"
    )


# ---------------------------------------------------------------------------
# Issue 97a29c75: wire the unconditional top-level-symbol-loss check into
# replace_lines/str_replace and name lost symbols explicitly in the removal
# report. A replace_lines/str_replace whose range fully removes an unrelated,
# already-correct top-level function/constant (consumed by OTHER files, never
# referenced in the same file) must name those symbols explicitly in the
# rejection message, and confirm_removals=true must still let the edit through.
# ---------------------------------------------------------------------------

def test_replace_lines_names_dropped_top_level_symbols(tmp_path, monkeypatch):
    """replace_lines whose range fully removes an unreferenced top-level
    function AND constant must name both symbols in the rejection."""
    monkeypatch.setattr(la, "CWD", tmp_path)
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
    # Lines 1-5 cover helper_func + SOME_CONST (and the blank line), replacing
    # them with an unrelated comment. Neither symbol is referenced in-file.
    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 1,
        "end": 5,
        "new_str": "# unrelated comment\n",
    })
    assert "The edit was NOT applied." in result
    assert "helper_func" in result
    assert "SOME_CONST" in result
    assert "permanently removes these top-level symbols" in result


def test_str_replace_names_dropped_top_level_symbols(tmp_path, monkeypatch):
    """str_replace whose old_str fully covers an unreferenced top-level
    function AND constant must name both symbols in the rejection."""
    monkeypatch.setattr(la, "CWD", tmp_path)
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
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": old_str,
        "new_str": "# unrelated comment\n",
    })
    assert "The edit was NOT applied." in result
    assert "helper_func" in result
    assert "SOME_CONST" in result


def test_replace_lines_confirm_removals_true_applies_dropped_symbol_edit(tmp_path, monkeypatch):
    """The bypass: the same edit that names dropped symbols must still apply
    when confirm_removals=true is passed (existing bypass behavior preserved)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
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
    result = la.run_tool("replace_lines", {
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


def test_replace_lines_no_full_symbol_removed_keeps_raw_report_only(tmp_path, monkeypatch):
    """Negative (a): removing lines from INSIDE a function body (no complete
    top-level symbol removed) must NOT trigger the new symbol-naming line."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def keeper():\n"
        "    do_thing()\n"
        "    time.sleep(10)\n"
        "    return\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    do_thing()\n",
    })
    # The edit is rejected (a true deletion of the time.sleep line), but it
    # must NOT carry the new named-symbol line because no complete top-level
    # symbol was removed.
    assert "permanently removes these top-level symbols" not in result


def test_str_replace_rename_names_old_symbol_as_removed(tmp_path, monkeypatch):
    """Negative (b): a str_replace that renames a top-level function
    (old_name -> new_name) must name old_name as removed (unconditional-drop
    semantics from the prerequisite story flag renames)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = "def old_name():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "def old_name():\n    return 1\n",
        "new_str": "def new_name():\n    return 1\n",
    })
    assert "The edit was NOT applied." in result
    assert "old_name" in result


def test_str_replace_blocks_constant_only_top_level_drop_without_confirm(tmp_path, monkeypatch):
    """A str_replace that fully removes a top-level constant assignment
    (no def/class touched) without confirm_removals must be BLOCKED: it
    returns an ERROR string naming the dropped constant, and the file on
    disk is left unchanged (still contains the constant).

    This is the var-only-drop case the dropped_defs-only gate used to let
    through. The gate must now check the combined dropped (defs + vars)
    list, matching create_file's existing overwrite guard.
    """
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
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


def test_str_replace_constant_only_drop_confirm_removals_true_applies(tmp_path, monkeypatch):
    """The bypass: the same constant-only drop that is blocked without the
    flag must still apply when confirm_removals=true is passed (existing
    bypass behavior preserved for the widened gate)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
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


def test_str_replace_constant_only_drop_names_only_the_constant(tmp_path, monkeypatch):
    """When a constant-only drop is blocked, the rejection must name the
    actual dropped symbol(s) from the combined `dropped` list and must NOT
    silently omit a var-only drop. The surviving def must not be falsely
    reported as removed."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "OLD_TIMEOUT = 30\n"
        "\n"
        "def keeper():\n"
        "    return 2\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "OLD_TIMEOUT = 30\n\n",
        "new_str": "",
    })
    assert "OLD_TIMEOUT" in result
    # The surviving def is not reported as removed.
    assert "keeper" not in result
