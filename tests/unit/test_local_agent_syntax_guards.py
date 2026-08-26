"""Tests for the local dispatch agent loop (scripts/local_agent.py): syntax-rejection escalation, off-task-drift, and churn/repetition guards in run_tool.

Split out of test_local_agent.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `la` module itself) moved to tests.unit._local_agent_test_helpers.
"""

import pytest

from tests.unit._local_agent_test_helpers import (
    _GENERIC_NUDGE,
    _init_git_repo,
    la,
    lar,
)


def test_safe_run_tool_recovers_missing_required_arg(tmp_path, monkeypatch):
    """A str_replace call missing old_str must not crash the agent — it should
    come back as a recoverable error string the model can react to."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "f.txt").write_text("some content")
    # No "old_str" key — run_tool would raise KeyError without the guard.
    result = la.safe_run_tool("str_replace", {"path": "f.txt"})
    assert isinstance(result, str)
    assert result.startswith("ERROR running str_replace")
    assert "KeyError" in result


def test_safe_run_tool_appends_required_args_hint_on_missing_key(tmp_path, monkeypatch):
    """Live validation run (2026-07-18, TDD_SPLIT_PRODUCTION_PLAN.md Phase 5):
    gpt-oss:20b called view_file with hallucinated {"line_start", "line_end"}
    keys instead of the declared {"path"}, got back a bare "ERROR running
    view_file: KeyError: 'path'", and needed 2-3 more malformed retries
    before self-correcting - tripping the per-target repetition guard into a
    park. The bare KeyError names what's missing but not the tool's actual
    required shape. Augment (not replace - test_safe_run_tool_recovers_
    missing_required_arg above must keep passing unchanged) the existing
    error with the schema's required keys and what was actually passed, so
    a weak model has enough information to correct itself in one retry."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "f.txt").write_text("some content")
    result = la.safe_run_tool("str_replace", {"path": "f.txt"})
    assert "requires" in result
    assert "old_str" in result
    assert "new_str" in result


def test_safe_run_tool_passes_through_normal_results(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.safe_run_tool("create_file", {"path": "new.txt", "content": "hi"})
    assert result == "created new.txt"
    assert (tmp_path / "new.txt").read_text() == "hi"


def test_safe_run_tool_handles_unknown_tool():
    assert la.safe_run_tool("bogus", {}) == "unknown tool bogus"


def test_search_tool_returns_actionable_steering_message(tmp_path, monkeypatch):
    """The model has no 'search' tool (live incident 2026-07-20, story
    93fdc371): gpt-oss:20b called a nonexistent 'search' tool 7 times while
    trying to locate a function definition, each time getting the bare
    generic 'unknown tool search' with no steering toward what actually
    exists (bash + grep/rg). Give this one specific case an actionable
    message instead of the generic fallback."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("search", {"query": "def foo"})
    assert result != "unknown tool search"
    assert "bash" in result
    assert "grep" in result or "rg" in result


def test_unknown_tool_other_than_search_unchanged(tmp_path, monkeypatch):
    """Regression guard: the search-specific steering message must not
    swallow the generic unknown-tool fallback for any other invalid name."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    assert la.run_tool("nonexistent_tool_xyz", {}) == "unknown tool nonexistent_tool_xyz"


def test_view_file_returns_requested_line_range(tmp_path, monkeypatch):
    """view_file with line_start/line_end on a file whose full content
    exceeds the 3000-char truncation must return the requested range, not
    just the head. Live incident 2026-07-20 (story 93fdc371): a 222-line
    file's target function was defined at line 114 but view_file's flat
    [:3000] truncation only ever showed roughly the first 63 lines — the
    function was architecturally unreachable no matter how many times
    view_file was called, because there was no way to ask for a later
    range."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    lines = [f"line {i} content padding padding padding\n" for i in range(1, 301)]
    (tmp_path / "big.py").write_text("".join(lines))
    result = la.run_tool("view_file", {"path": "big.py", "line_start": 200, "line_end": 205})
    assert " 200| line 200 content padding padding padding\n" in result
    assert " 205| line 205 content padding padding padding\n" in result
    assert "line 1 content" not in result
    assert "line 300 content" not in result


def test_view_file_without_range_on_small_file_is_unchanged(tmp_path, monkeypatch):
    """Regression guard: a file well under the truncation limit must return
    byte-for-byte the same string with or without this change — the
    continuation hint must not leak into the untruncated case."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    result = la.run_tool("view_file", {"path": "small.py"})
    assert result == "   1| x = 1\n   2| y = 2\n"


def test_view_file_without_range_on_large_file_includes_continuation_hint(tmp_path, monkeypatch):
    """A large file with no range still returns the truncated head (existing
    behavior preserved) but now also tells the model it can ask for more via
    line_start/line_end and how many lines the file actually has."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    lines = [f"line {i} content padding padding padding\n" for i in range(1, 301)]
    (tmp_path / "big.py").write_text("".join(lines))
    result = la.run_tool("view_file", {"path": "big.py"})
    assert result.startswith("   1| line 1 content")
    assert "line_start" in result
    assert "line_end" in result
    assert "300" in result


def test_view_file_line_start_beyond_file_length(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    result = la.run_tool("view_file", {"path": "small.py", "line_start": 50, "line_end": 60})
    assert result.startswith("ERROR")


def test_view_file_line_end_less_than_line_start(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\ny = 3\n")
    result = la.run_tool("view_file", {"path": "small.py", "line_start": 3, "line_end": 1})
    assert result.startswith("ERROR")


def test_view_file_line_start_zero_is_rejected(tmp_path, monkeypatch):
    """line_start is 1-indexed; 0 is a plausible 0-indexed-thinking model
    error and must not silently slice to an empty result (negative-index
    slicing bug caught in review — the whole point of this fix is replacing
    silent truncation with an actionable error, not swapping one silent
    failure for another)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("a\nb\nc\nd\ne\n")
    result = la.run_tool("view_file", {"path": "small.py", "line_start": 0, "line_end": 2})
    assert result.startswith("ERROR")


def test_view_file_line_start_negative_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("a\nb\nc\nd\ne\n")
    result = la.run_tool("view_file", {"path": "small.py", "line_start": -1, "line_end": 2})
    assert result.startswith("ERROR")


def test_view_file_missing_path_still_required():
    """path must stay the only required key — omitting it (even while
    passing line_start/line_end) must still hit the existing missing-
    required-arg recovery path in safe_run_tool, not a new failure mode."""
    result = la.safe_run_tool("view_file", {"line_start": 1, "line_end": 2})
    assert result.startswith("ERROR running view_file")
    assert "path" in result


def test_create_file_rejects_invalid_python_syntax_diff_artifact(tmp_path, monkeypatch):
    """A stray unified-diff leading '+' pasted into content must be rejected
    before it lands on disk — this is a defense-in-depth guard against a
    class of malformed model output, not an attempt to explain why it
    happens."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    bad_content = "+def foo():\n+    return 1\n"
    result = la.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_create_file_rejects_invalid_python_syntax_dangling_triple_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    bad_content = '"""unterminated docstring\ndef foo():\n    pass\n'
    result = la.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_str_replace_rejects_edit_that_produces_invalid_python_syntax(tmp_path, monkeypatch):
    """A rejected edit must not partially apply — the file's on-disk content
    must be byte-for-byte unchanged from before the call."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = "def foo():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "+    return 1",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert (tmp_path / "mod.py").read_text() == original


def test_create_file_rejects_return_outside_function(tmp_path, monkeypatch):
    """ast.parse() alone accepts this (it only validates grammar, not that
    `return` sits inside a function) - observed live 2026-07-15: a dedented
    `for` loop landed a `return` at module scope, ast.parse() let it through,
    and the file reached the groundtruth oracle as an import-breaking
    SyntaxError only python's compile() step catches. The guard must use
    compile(), not ast.parse(), to close this gap."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    bad_content = (
        "def foo():\n"
        "    x = 1\n"
        "for i in range(3):\n"
        "    y = i\n"
        "    return y\n"
    )
    result = la.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_try_repair_indentation_fixes_decorator_dedent():
    """The 14B's reproducible decoding defect: after `@property` it drops the
    leading indentation on the next line, writing `    @property` then
    `def size(self):` at column 0 - a SyntaxError (unexpected unindent) it
    resubmits byte-identical until it parks (lru_cache t7/t8/t10/t11). A
    prompt-level worked example did NOT prevent it (t11 - decoding-level, not
    understanding-level). The deterministic repair re-indents the dedented
    line to match the preceding decorator and re-validates."""
    broken = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self._data = {}\n"
        "    @property\n"
        "def size(self):\n"
        "        return len(self._data)\n"
    )
    repair = la._try_repair_indentation(broken)
    assert repair is not None
    repaired, note = repair
    # The def must now sit at 4 spaces, matching the @property decorator.
    assert "    @property\n    def size(self):\n" in repaired
    assert "auto-reindented" in note
    # And the repaired content must actually compile.
    compile(repaired, "<test>", "exec")


def test_try_repair_indentation_fixes_multiple_dedented_decorators():
    """The decoding defect drops the `def` line after EVERY decorator in the
    file, not just the first (observed live, 2026-07-17, lru_cache: both the
    `@property` getter `def size` AND the `@size.setter` `def size` were
    dedented to column 0). The single-line repair fixed the getter, but the
    setter still broke compile, so the repair returned None and correct code
    was rejected every retry until the wall-clock park. The repair must
    ITERATE: fix one dedented def, re-compile, fix the next, until clean."""
    broken = (
        "from collections import OrderedDict\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self._data = OrderedDict()\n"
        "    @property\n"
        "def size(self):\n"
        "        return len(self._data)\n"
        "    @size.setter\n"
        "def size(self, value):\n"
        "        raise AttributeError('read-only')\n"
    )
    repair = la._try_repair_indentation(broken)
    assert repair is not None, "multi-decorator dedent must be repaired, not rejected"
    repaired, note = repair
    # BOTH dedented defs must now sit at 4 spaces, matching their decorators.
    assert "    @property\n    def size(self):\n" in repaired
    assert "    @size.setter\n    def size(self, value):\n" in repaired
    assert "auto-reindented" in note
    # And the repaired content must actually compile.
    compile(repaired, "<test>", "exec")


def test_create_file_auto_repairs_decorator_dedent_and_writes(tmp_path, monkeypatch):
    """When create_file receives the decorator-dedent defect, it must write the
    REPAIRED content to disk (not reject it into the death-loop) and tell the
    model what it did - no silent mutation."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    broken = (
        "class C:\n"
        "    @property\n"
        "def size(self):\n"
        "        return 1\n"
    )
    result = la.run_tool("create_file", {"path": "mod.py", "content": broken})
    assert result.startswith("created mod.py")
    assert "auto-reindented" in result
    on_disk = (tmp_path / "mod.py").read_text()
    assert "    @property\n    def size(self):\n" in on_disk
    compile(on_disk, "<test>", "exec")


def test_create_file_does_not_auto_repair_non_indentation_error(tmp_path, monkeypatch):
    """Auto-repair is scoped to IndentationError only. A non-indentation
    SyntaxError (return outside a function - the original reason this guard
    uses compile() not ast.parse()) must still be rejected, not silently
    rewritten."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad = "def foo():\n    x = 1\nfor i in range(3):\n    return i\n"
    result = la.run_tool("create_file", {"path": "mod.py", "content": bad})
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_try_repair_indentation_returns_none_when_no_preceding_line():
    """A top-level indented line has no preceding non-blank line to take a
    target indentation from - the repair cannot apply, so it returns None and
    the normal rejection path handles it."""
    assert la._try_repair_indentation("    x = 1\n") is None


def test_try_repair_indentation_returns_none_for_valid_content():
    """Already-valid content is not a repair candidate (no error to fix)."""
    assert la._try_repair_indentation("def foo():\n    return 1\n") is None


def test_str_replace_auto_repairs_indentation(tmp_path, monkeypatch):
    """The same auto-repair applies to str_replace edits that produce an
    indentation error - the repaired result is written, not rejected."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    (tmp_path / "mod.py").write_text("class C:\n    def m(self):\n        return 1\n")
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    def m(self):\n        return 1\n",
        "new_str": "    def m(self):\n        return 1\n    @property\ndef size(self):\n        return 2\n",
    })
    assert result.startswith("edited mod.py")
    assert "auto-reindented" in result
    on_disk = (tmp_path / "mod.py").read_text()
    assert "    @property\n    def size(self):\n" in on_disk
    compile(on_disk, "<test>", "exec")


def test_no_tool_nudge_escalates_after_consecutive_turns():
    """The static 'Call a tool now' nudge cannot break a narration loop where
    the model is stuck on a self-inflicted phantom failure (its own test
    asserts wrong behavior the correct impl can never satisfy). The nudge
    escalates: plain call-to-action for the first two consecutive no-tool
    turns, then behavioral guidance telling the model a stuck self-test may
    be wrong and to fix the test, not the impl."""
    assert la._no_tool_nudge(1) == "Call a tool now (do not write prose)."
    assert la._no_tool_nudge(2) == "Call a tool now (do not write prose)."
    escalated = la._no_tool_nudge(3)
    assert "failing test that you wrote" in escalated
    assert "fix or delete the failing test" in escalated
    # Still escalates at higher counts (no regression to the plain nudge).
    assert la._no_tool_nudge(5) == escalated




def test_no_tool_nudge_targets_done_tool_on_completion_prose():
    """When the model's own turn narrates completion instead of calling a
    tool, the early (consecutive 1-2) nudge must direct it to call `done`
    specifically rather than the generic call-to-action - a narrating model
    that never gets told which tool to call can burn all the way to
    NO_TOOL_CAP before self-correcting (live 2026-07-29: gpt-oss:20b said
    "All done." as prose, not a tool call, and only recovered because the
    generic nudge happened to work that time)."""
    for phrase in ("All done.", "I'm done.", "I am done.", "Finished.", "All finished."):
        for c in (1, 2):
            nudge = la._no_tool_nudge(c, content=phrase)
            assert "done" in nudge.lower() and nudge != _GENERIC_NUDGE, (c, phrase, nudge)


def test_no_tool_nudge_non_completion_prose_stays_generic():
    assert la._no_tool_nudge(1, content="Let me check the file.") == _GENERIC_NUDGE
    assert la._no_tool_nudge(2, content="") == _GENERIC_NUDGE


def test_no_tool_nudge_negation_not_done_or_not_finished_stays_generic():
    """'not done yet'/'not finished' contain the negated word but are NOT
    completion - must stay generic, not misfire the targeted nudge."""
    assert la._no_tool_nudge(1, content="not done yet, still working") == _GENERIC_NUDGE
    assert la._no_tool_nudge(1, content="not finished") == _GENERIC_NUDGE
    assert la._no_tool_nudge(2, content="not finished") == _GENERIC_NUDGE


def test_no_tool_nudge_compound_word_unfinished_stays_generic():
    """Regression (caught in review 2026-07-29, twice missed by an
    acceptance oracle that only exercised 'not done yet'/'not finished'):
    plain substring search for "finished" also matches inside "unfinished"
    and "refinished", so a model reporting genuinely incomplete work ("this
    is unfinished") was wrongly told to call `done`. Word-boundary matching
    must not treat a compound word as containing the bare phrase."""
    assert la._no_tool_nudge(1, content="This is unfinished work, more to do") == _GENERIC_NUDGE
    assert la._no_tool_nudge(2, content="unfinished") == _GENERIC_NUDGE
    assert la._no_tool_nudge(1, content="the section was refinished") == _GENERIC_NUDGE


def test_no_tool_nudge_escalation_unchanged_regardless_of_completion_content():
    """The consecutive>=3 escalation path must stay exactly as before,
    whether or not the content indicates completion - only the early
    (1-2) turns get the new targeted-done behavior."""
    escalated = la._no_tool_nudge(3)
    assert la._no_tool_nudge(3, content="All done.") == escalated
    assert la._no_tool_nudge(5, content="All done.") == escalated


def test_no_tool_nudge_backward_compatible_without_content_arg():
    assert la._no_tool_nudge(1) == _GENERIC_NUDGE
    assert la._no_tool_nudge(2) == _GENERIC_NUDGE


def test_no_tool_call_site_wires_content_into_targeted_nudge(tmp_path, monkeypatch, capsys):
    """DYNAMIC integration check, not just the function in isolation: drive
    the real agent loop (main()) with a mocked chat that returns "All
    done." prose and no tool call. If the production call site does not
    pass the assistant's content through to _no_tool_nudge, the done-prose
    detection is dead code at runtime even though the unit tests above all
    pass - this is exactly the gap a unit-only fixture leaves open."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NO_TOOL_CAP", 5)
    monkeypatch.setattr(la, "MAX_STEPS", 40)

    seen = []

    def _done_chat(messages):
        seen.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "All done.", "tool_calls": []}

    monkeypatch.setattr(la, "chat", _done_chat)

    rc = la.main()
    capsys.readouterr()

    assert len(seen) >= 2, f"loop never reached a 2nd chat call (rc={rc})"
    nudge = seen[1][-1]
    assert nudge.get("role") == "user"
    assert "done" in nudge["content"].lower() and nudge["content"] != _GENERIC_NUDGE, (
        "the agent loop did not direct the done-prose turn to call done -- "
        "the production call site is not passing content through: "
        + repr(nudge["content"])
    )


def test_local_agent_narration_cap_parks_after_consecutive_no_tool_turns(
    tmp_path, monkeypatch, capsys
):
    """A model that emits prose instead of tool calls indefinitely (the
    t12/t13 narration loop) must park after NO_TOOL_CAP consecutive no-tool
    turns, not burn the full MAX_STEPS budget. The cap is a deterministic
    safety net under the escalating nudge: even when the nudge fails to
    recover the run, the waste is bounded."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NO_TOOL_CAP", 5)
    monkeypatch.setattr(la, "MAX_STEPS", 40)

    # Every turn returns pure prose, no tool_calls, and plain content so
    # recover_tool_calls() finds nothing to recover - exactly the narration
    # collapse observed in t13 (turns 35+ after the pytest failure).
    def _prose_chat(messages):
        return {"role": "assistant",
                "content": "Next I will run the full test suite to confirm.",
                "tool_calls": []}

    monkeypatch.setattr(la, "chat", _prose_chat)

    rc = la.main()

    out = capsys.readouterr().out
    assert rc == 2, f"expected parking exit 2, got {rc}\noutput: {out!r}"
    assert "narration cap (5 consecutive no-tool turns) reached; parking" in out, (
        f"expected narration-cap park line, output: {out!r}"
    )
    # The run must stop at the cap (5 chat calls), nowhere near MAX_STEPS=40.
    # _prose_chat doesn't record calls, so count the [step N] no-tool lines.
    assert out.count("no tool call (") == 5, (
        f"expected exactly 5 no-tool turns before the cap, output: {out!r}"
    )


def test_create_file_accepts_valid_python_syntax(tmp_path, monkeypatch):
    """Regression: valid Python content must still write exactly as before."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("create_file", {"path": "mod.py", "content": "def foo():\n    return 1\n"})
    assert result == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "def foo():\n    return 1\n"


def test_str_replace_rejects_edit_that_orphans_a_referenced_variable(tmp_path, monkeypatch):
    """Reproduces the exact live failure mode from two separate local models
    (gpt-oss:20b deleting `plan_role_config = _plan_role_config(plan_name)`,
    qwen3-coder:30b deleting `branch = ...`/`worktree = ...`) while a
    reference to the deleted name survived elsewhere in the same function -
    both landed a NameError/UnboundLocalError in production code that
    compile() alone cannot catch (undefined names are a runtime error, not a
    SyntaxError). The edit must be rejected and the file left unchanged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def review_story(x):\n"
        "    worktree = x.get('worktree', '')\n"
        "    if x.get('flag'):\n"
        "        return worktree\n"
        "    return None\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    worktree = x.get('worktree', '')\n    if x.get('flag'):",
        "new_str": "    if x.get('flag'):",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "worktree" in result
    assert "review_story" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_replace_lines_rejects_edit_that_orphans_a_referenced_variable(tmp_path, monkeypatch):
    """Same failure mode as the str_replace case above, via replace_lines -
    both mutating-edit tools must be covered since either can produce this
    class of mistake."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def f(x):\n"
        "    branch = f'agent/{x}'\n"
        "    worktree = x\n"
        "    return _run(worktree, branch)\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 2,
        "end": 3,
        "new_str": "    # guard removed here\n",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "branch" in result
    assert "worktree" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_replace_lines_rejects_edit_that_deletes_a_called_module_level_def(
    tmp_path, monkeypatch,
):
    """Reproduces the live MODE-29-REVIEW-STORY-LOCK-GUARD incident
    (2026-07-22): a replace_lines edit deleted only the
    `def _review_story_impl(...):` line itself (replacing it with a blank
    line) while its ~300-line body and its caller's
    `return _review_story_impl(...)` both survived untouched. Because the
    orphaned body stayed correctly indented as trailing (unreachable) code
    inside the CALLER's function, the result is syntactically valid Python
    - compile() accepts it - so only a NameError surfaces, at runtime, on
    every call. The existing orphaned-variable check
    (_newly_undefined_names) only tracks function-local Name-Store/Load
    bindings and does not see a deleted module-level `def`; this is a
    distinct check. The edit must be rejected and the file left unchanged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "def review_story(plan_name, story_key):\n"
        "    with _plan_lock(plan_name) as acquired:\n"
        "        if not acquired:\n"
        "            return {\"ok\": True}\n"
        "        return _review_story_impl(plan_name, story_key)\n"
        "\n"
        "\n"
        "def _review_story_impl(plan_name, story_key):\n"
        "    return {\"plan\": plan_name, \"story\": story_key}\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 8,
        "end": 8,
        "new_str": "",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "_review_story_impl" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_replace_lines_rejects_edit_that_deletes_a_read_module_level_var(
    tmp_path, monkeypatch,
):
    """Reproduces the live MODE-43 incident (2026-07-30,
    TRANSPORT-ALIAS-READERS): a replace_lines edit on
    scripts/local_agent_oracle.py replaced the module-level
    `TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))` line with a
    duplicate of the preceding `NUM_CTX = ...` line, deleting the `TIMEOUT`
    assignment while every later reference to `TIMEOUT` survived. compile()
    accepts it (no SyntaxError); only a runtime NameError surfaces. The existing
    guards only cover function-local names (_newly_undefined_names) and
    module-level def/class (_newly_undefined_module_defs) - NEITHER sees a
    deleted module-level variable assignment. The edit must be rejected and the
    file left unchanged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    original = (
        "import os\n"
        "NUM_CTX = int(os.environ.get('LOCAL_AGENT_NUM_CTX', '16384'))\n"
        "TIMEOUT = float(os.environ.get('LOCAL_AGENT_TIMEOUT', '900'))\n"
        "MAX_STEPS = int(os.environ.get('LOCAL_AGENT_MAX_STEPS', '40'))\n"
        "\n"
        "def run():\n"
        "    deadline = time.time() + TIMEOUT\n"
        "    return deadline\n"
    )
    (tmp_path / "mod.py").write_text(original)
    # The destructive edit: line 3 (TIMEOUT) replaced with a duplicate NUM_CTX.
    result = la.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 3,
        "end": 3,
        "new_str": "NUM_CTX = int(os.environ.get('PIPELINE_TRANSPORT_NUM_CTX', '16384'))",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "TIMEOUT" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_orphaned_module_var_check_accepts_removal_with_all_uses(
    tmp_path, monkeypatch,
):
    """Boundary: a legitimate refactor that removes a module-level variable
    together with every reference to it must NOT be rejected - only a deleted
    assignment with a SURVIVING reference is a bug."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text(
        "TIMEOUT = float(900)\n"
        "def run():\n"
        "    return TIMEOUT\n"
    )
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "TIMEOUT = float(900)\ndef run():\n    return TIMEOUT\n",
        "new_str": "def run():\n    return 900.0\n",
    })
    assert result == "edited mod.py"
    assert (tmp_path / "mod.py").read_text() == "def run():\n    return 900.0\n"


def test_orphaned_variable_check_accepts_edit_that_removes_assignment_and_all_uses(
    tmp_path, monkeypatch,
):
    """Boundary: a legitimate refactor that removes BOTH the assignment and
    every reference to it together must NOT be falsely rejected - only a
    deleted assignment with a SURVIVING reference is a bug."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text(
        "def f(x):\n"
        "    unused = x\n"
        "    return unused\n"
    )
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    unused = x\n    return unused",
        "new_str": "    return x",
    })
    assert result == "edited mod.py"
    assert (tmp_path / "mod.py").read_text() == "def f(x):\n    return x\n"


def test_orphaned_variable_check_ignores_names_already_broken_before_the_edit(
    tmp_path, monkeypatch,
):
    """A name that was never assigned in the OLD content (already broken,
    not this edit's fault) must not be flagged - the check only blames an
    edit for a reference it orphaned, not for pre-existing breakage
    elsewhere in the file."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text(
        "def f(x):\n"
        "    return already_undefined + x\n"
    )
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "return already_undefined + x",
        "new_str": "return already_undefined + x + 1",
    })
    assert result == "edited mod.py"


def test_orphaned_variable_check_only_applies_to_py_paths(tmp_path, monkeypatch):
    """A non-.py path must never be checked - mirrors the syntax check's own
    .py-only scoping."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "notes.txt").write_text(
        "worktree = get()\nif flag:\n    return worktree\n"
    )
    result = la.run_tool("str_replace", {
        "path": "notes.txt",
        "old_str": "worktree = get()\nif flag:",
        "new_str": "if flag:",
    })
    assert result == "edited notes.txt"


def test_str_replace_accepts_edit_that_keeps_valid_python_syntax(tmp_path, monkeypatch):
    """Regression: a valid edit must still apply exactly as before."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def foo():\n    return 1\n")
    result = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "return 1",
        "new_str": "return 2",
    })
    assert result == "edited mod.py"
    assert (tmp_path / "mod.py").read_text() == "def foo():\n    return 2\n"


def test_create_file_syntax_check_only_applies_to_py_paths(tmp_path, monkeypatch):
    """A non-.py path with content that looks like broken Python must not be
    rejected — the ast.parse check is scoped to .py targets only."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    bad_python_looking_content = '"""unterminated\ndef foo(:\n    +return 1\n'
    result = la.run_tool("create_file", {"path": "README.md", "content": bad_python_looking_content})
    assert result == "created README.md"
    assert (tmp_path / "README.md").read_text() == bad_python_looking_content


def test_create_file_empty_content_on_py_path_is_not_rejected(tmp_path, monkeypatch):
    """An empty file is valid Python (ast.parse('') does not raise) — must
    not be a false-positive rejection."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("create_file", {"path": "empty.py"})
    assert result == "created empty.py"
    assert (tmp_path / "empty.py").read_text() == ""


def test_create_file_overwrites_a_file_it_created_earlier_this_run(tmp_path, monkeypatch):
    """A model that mistakenly calls create_file again on a path it already
    successfully created THIS run should be allowed to overwrite it (a full
    rewrite is often the natural recovery strategy for a weak model that
    can't construct a correct str_replace old_str) - the non-destructive
    guard exists to protect PRE-EXISTING repo/seed files from being
    clobbered, not files the agent itself just wrote. Observed live
    2026-07-15 (lru_cache): a model stuck alternating rejected create_file
    and rejected str_replace calls for dozens of steps, never finishing."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_CREATED_THIS_RUN", set())
    r1 = la.run_tool("create_file", {"path": "mod.py", "content": "x = 1\n"})
    assert r1 == "created mod.py"
    r2 = la.run_tool("create_file", {"path": "mod.py", "content": "x = 2\n"})
    assert r2 == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "x = 2\n"


def test_create_file_rejects_overwrite_of_unseen_pre_existing_file(tmp_path, monkeypatch):
    """Regression: a file that exists on disk, was NOT created via create_file
    this run, and has NOT been read via view_file this run must still be
    protected from blind clobber. Only a file the agent authored or has read
    this run is eligible for overwrite. The refusal steers to view_file (read
    before you overwrite), not str_replace — a weak model reliably cannot
    build a matching old_str, so directing it to str_replace traps it in the
    surgical-edit deadlock (observed live: interval_merge resume, 28+ rejected
    str_replace cycles, 2026-07-16)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "mod.py").write_text("x = 1\n")
    result = la.run_tool("create_file", {"path": "mod.py", "content": "x = 2\n"})
    assert result == (
        "ERROR: mod.py already exists and is non-empty. Use view_file to read "
        "it first, then create_file to overwrite it with the full corrected "
        "contents."
    )
    assert (tmp_path / "mod.py").read_text() == "x = 1\n"


def test_create_file_overwrites_a_pre_existing_file_after_view_file(tmp_path, monkeypatch):
    """Informed overwrite: once the agent has read a pre-existing file via
    view_file this run, create_file may overwrite it with a full rewrite. This
    unblocks the whole-file recovery path on a step-cap RESUME, where the impl
    and test files already exist on disk from the interrupted run (so they are
    not in _CREATED_THIS_RUN) — without it, the create_file guard forces the
    weak model onto str_replace and the surgical-edit deadlock (interval_merge
    resume, 2026-07-16). The blind-clobber protection is preserved: only a
    file the agent has actually read this run becomes eligible."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    (tmp_path / "mod.py").write_text("def merge():\n    raise NotImplementedError\n")
    la.run_tool("view_file", {"path": "mod.py"})
    result = la.run_tool("create_file", {"path": "mod.py", "content": "def merge():\n    return []\n"})
    assert result == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "def merge():\n    return []\n"


def test_create_file_rejects_overwrite_that_silently_drops_top_level_defs(tmp_path, monkeypatch):
    """Regression for the live 2026-08-07 incident (w3a-effective-config-
    provenance, story f7fd39c4): a create_file rewrite of a 5-function file
    kept only 1 function, silently dropping the other 4 - all public API
    consumed from OTHER modules, so nothing in the file's own body still
    called them and _newly_undefined_module_defs's reference check never
    fired. Once the agent has view_file'd a pre-existing file (eligible for
    overwrite), a create_file that would drop top-level def/class present in
    the old content but missing from the new content must be rejected unless
    confirm_removals=true, mirroring replace_lines's confirm_removals gate."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())
    original = (
        "def ignored_env_vars_present():\n    return []\n\n\n"
        "def read_plist_env():\n    return {}\n\n\n"
        "def read_mcp_server_env():\n    return {}\n\n\n"
        "def _scheduler_plist_path():\n    return None\n\n\n"
        "def _claude_json_path():\n    return None\n"
    )
    (tmp_path / "mod.py").write_text(original)
    la.run_tool("view_file", {"path": "mod.py"})
    truncated = "def ignored_env_vars_present():\n    return []\n"
    result = la.run_tool("create_file", {"path": "mod.py", "content": truncated})
    assert result.startswith("ERROR: this create_file overwrite of mod.py would silently drop")
    for name in ("read_plist_env", "read_mcp_server_env", "_scheduler_plist_path", "_claude_json_path"):
        assert name in result
    assert (tmp_path / "mod.py").read_text() == original

    confirmed = la.run_tool(
        "create_file", {"path": "mod.py", "content": truncated, "confirm_removals": True}
    )
    assert confirmed == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == truncated


def test_syntax_error_message_includes_lineno_and_offending_line(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: the rejection must name the exact line and quote the
    offending line plus up to 2 lines of context either side, verbatim from
    the content the model actually submitted — not a repaired version."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "+def bar():\n"
        "    return 2\n"
    )
    import ast
    try:
        ast.parse(bad_content)
        pytest.fail("fixture must itself be invalid Python")
    except SyntaxError as e:
        expected_lineno = e.lineno
    result = la.run_tool("create_file", {"path": "ctx.py", "content": bad_content})
    assert result.startswith("ERROR")
    assert str(expected_lineno) in result
    assert "+def bar():" in result
    assert "return 1" in result
    assert "return 2" in result


