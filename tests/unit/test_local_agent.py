"""Tests for the local dispatch agent loop (scripts/local_agent.py).

Imported as a module; it only requires LOCAL_AGENT_MODEL in the environment at
import time, so set that before importing. External boundaries (the Ollama
HTTP call) are not exercised here — these cover the pure-logic helpers.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


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
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
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
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
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
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
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


_GENERIC_NUDGE = "Call a tool now (do not write prose)."


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
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
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


def test_second_consecutive_syntax_rejection_same_path_carries_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: resubmitting the same broken content for the same path
    must escalate from the second consecutive rejection onward, telling the
    model to regenerate from scratch rather than retry the same content."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    first = la.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit" not in first.lower()
    second = la.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" in second.lower()


def test_second_consecutive_str_replace_rejection_on_large_file_suggests_anchored_edit(
    tmp_path, monkeypatch
):
    """SYNTAX-NUDGE (large file): a str_replace rejection on an existing
    file above the size threshold must NOT get the 'regenerate the entire
    file' nudge - regenerating a large file from scratch risks corrupting
    the untouched majority of it. It should get a smaller-anchored-edit
    nudge instead."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    lines = [f"x{i} = {i}\n" for i in range(600)]
    lines.append("def marker():\n    return 1\n")
    (tmp_path / "big.py").write_text("".join(lines))
    old_str = "def marker():\n    return 1"
    new_str = "+def marker():\n    return 1"
    first = la.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert first.startswith("ERROR")
    second = la.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" not in second.lower()
    assert "smaller" in second.lower()


def test_rejection_for_different_path_does_not_inherit_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a rejection for a DIFFERENT path in
    between must not carry the escalation — the counter is per-path."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "a.py", "content": bad_content})
    result_b = la.run_tool("create_file", {"path": "b.py", "content": bad_content})
    assert "do not resubmit" not in result_b.lower()
    result_a_again = la.run_tool("create_file", {"path": "a.py", "content": bad_content})
    assert "do not resubmit" in result_a_again.lower()


def test_successful_write_resets_syntax_rejection_counter(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a successful write to a path resets its
    consecutive-rejection counter, so a later rejection for that same path
    starts fresh (no escalation) instead of carrying over stale state."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "reset.py", "content": bad_content})
    good = la.run_tool("create_file", {"path": "reset.py", "content": "def foo():\n    return 1\n"})
    assert good == "created reset.py"
    again = la.run_tool("str_replace", {
        "path": "reset.py",
        "old_str": "    return 1",
        "new_str": "+    return 1",
    })
    assert again.startswith("ERROR")
    assert "do not resubmit" not in again.lower()


def test_syntax_rejection_never_writes_file_even_with_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE regression guard: the rejection must still return the
    exact original ERROR-prefixed contract and must NEVER write the file —
    not the submitted content, not a repaired version — even once escalated.
    Guards against silently reintroducing auto-repair."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    result = la.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    assert result.startswith("ERROR")
    assert "do not resubmit" in result.lower()
    assert not (tmp_path / "guard.py").exists() or not (tmp_path / "guard.py").read_text().strip()


def test_valid_python_writes_never_trigger_escalation_text(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: valid .py content must remain entirely unaffected by the
    new rejection-message/escalation machinery."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", {})
    result1 = la.run_tool("create_file", {"path": "ok.py", "content": "x = 1\n"})
    assert result1 == "created ok.py"
    result2 = la.run_tool("str_replace", {"path": "ok.py", "old_str": "x = 1", "new_str": "x = 2"})
    assert result2 == "edited ok.py"


class _FakeProc:
    """A stand-in CompletedProcess so the bash handler can format output."""

    def __init__(self):
        self.stdout = "ok"
        self.stderr = ""


@pytest.mark.parametrize("cmd", [
    "git reset --hard",
    "git reset --hard HEAD",
    "git reset --hard b5063c7",
    "git reset  --hard origin/master",
    "git clean -f",
    "git clean -fd",
    "git clean -fdx",
    "git clean --force",
    "git clean -xf",      # force, f not first — must still be caught
    "git clean -df",
    "git clean -xdf",
    "git checkout -- .",
    "git checkout -- src/app.py",
    "git checkout HEAD -- src/app.py",
    "git restore src/app.py",
    # Destructive op buried in a compound command must still be caught.
    "git status && git reset --hard b5063c7",
    "cd sub && git clean -fd && git status",
    "git status\ngit reset --hard b5063c7",   # destructive on its own line IS caught
])
def test_bash_blocks_destructive_git_ops(tmp_path, monkeypatch, cmd):
    """A destructive git op (one that discards the branch's WIP commits or
    working-tree changes) must be refused before subprocess.run is reached —
    a blind-rework agent running `git reset --hard <master>` once threw away
    its own tests-passed WIP."""
    monkeypatch.setattr(la, "CWD", tmp_path)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not be called for a destructive git op")

    monkeypatch.setattr(la.subprocess, "run", _boom)
    result = la.run_tool("bash", {"command": cmd})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "blocked" in result.lower()


@pytest.mark.parametrize("cmd", [
    "git status",
    "git add -A",
    "git commit -m 'WIP'",
    "git diff",
    "git log --oneline",
    "git checkout feature-branch",   # branch switch is NOT a discard
    "git reset HEAD src/app.py",      # unstage only, no --hard
    "git checkout --theirs src/app.py",  # merge opt, not the pathsep `--`
    "pytest -q",
    "git stash",
    "git rebase origin/master",
    # Multi-line: a safe `git reset HEAD <path>` on one line must not be
    # attributed to a `--hard` token on an unrelated later line.
    "git reset HEAD src/app.py\necho --hard-done-here\ngit status",
])
def test_bash_passes_non_destructive_commands_through(tmp_path, monkeypatch, cmd):
    """Non-destructive git ops and ordinary commands must still run."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    seen = {}

    def _fake_run(c, **k):
        seen["cmd"] = c
        return _FakeProc()

    monkeypatch.setattr(la.subprocess, "run", _fake_run)
    result = la.run_tool("bash", {"command": cmd})
    assert seen["cmd"] == cmd          # actually dispatched
    assert result == "ok"              # ran and returned output


# ---------- restore_file tool (2026-07-22 harness-improvement plan) ----------
# The destructive-git-op guard correctly blocks `git reset --hard`/`git
# checkout -- <path>`, but observed live: a model that WANTS exactly that (its
# own edits to one file went wrong and it wants a clean slate) got blocked
# three times with no alternative it could actually use, and spent the rest
# of its step budget stuck. restore_file is the safe, scoped escape hatch:
# git checkout HEAD -- <path>, one file only, reachable directly (not through
# the blocked bash patterns).

def test_restore_file_reverts_to_last_commit(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "a.py"
    f.write_text("original\n")
    subprocess.run(["git", "add", "a.py"], check=False, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=False, cwd=tmp_path, capture_output=True)
    f.write_text("a mess the model made\n")

    result = la.run_tool("restore_file", {"path": "a.py"})

    assert not result.startswith("ERROR"), f"unexpected error: {result}"
    assert f.read_text() == "original\n"


def test_restore_file_leaves_other_files_untouched(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("original a\n")
    b.write_text("original b\n")
    subprocess.run(["git", "add", "-A"], check=False, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=False, cwd=tmp_path, capture_output=True)
    a.write_text("messed up a\n")
    b.write_text("a real in-progress edit to b\n")

    la.run_tool("restore_file", {"path": "a.py"})

    assert a.read_text() == "original a\n"
    assert b.read_text() == "a real in-progress edit to b\n"


def test_restore_file_requires_path():
    result = la.run_tool("restore_file", {})
    assert result.startswith("ERROR")


def test_restore_file_reports_git_error(tmp_path, monkeypatch):
    """A path git can't resolve (no repo, no such path in history) must
    surface as a clear ERROR, not crash the loop."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("restore_file", {"path": "nonexistent.py"})
    assert result.startswith("ERROR")


def test_destructive_git_op_error_points_to_restore_file(tmp_path, monkeypatch):
    """The blocked-op message must name restore_file as the alternative for
    exactly the intent it's blocking (discard my own edits to one file)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("bash", {"command": "git reset --hard HEAD"})
    assert "restore_file" in result


# ---------- net-progress guard (2026-07-22) ----------
# The per-target and read-heavy guards both reset on any successful mutation,
# and the no-tool-call cap only counts CONSECUTIVE narration turns. A run
# that alternates one edit with long stretches of distinct, non-repeating
# inspection and isolated give-up narration evades both indefinitely -
# observed live: 50 of 60 steps with zero further edits after an early one,
# no guard ever fired. This guard tracks steps since the last successful
# mutation directly.

def test_net_progress_guard_parks_after_max_steps_with_no_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 3)
    # Large so the read-heavy guard doesn't also fire and confuse the signal.
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(10)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 3, f"expected net-progress park, got rc={rc}\noutput: {out!r}"
    assert "no successful edit in 3 steps" in out, f"output: {out!r}"
    assert len(calls) == 3, (  # steps 0,1,2 run; the check at step 3 parks before calling chat()
        f"expected exactly 3 chat() calls before parking, got {len(calls)}\noutput: {out!r}"
    )


def test_net_progress_guard_resets_on_successful_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 3)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = (
        [("bash", {"command": "cat a"}), ("bash", {"command": "cat b"})]
        + [("create_file", {"path": "new.py", "content": "# real code\n"})]
        + [("bash", {"command": "cat c"})]
        + [("done", {"summary": "wrote the module"})]
    )
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, (
        f"the mutation at call 3 should reset the counter so the run "
        f"reaches done, not park; got rc={rc}\noutput: {out!r}"
    )
    assert "no successful edit" not in out, f"output: {out!r}"


def test_net_progress_guard_park_disabled_renudges_instead_of_terminating(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 2)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(8)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("no successful edit in") >= 2, (
        f"expected the guard to re-fire (not just once), output: {out!r}"
    )
    assert len(calls) > 3, (
        f"disabled parking should let the run continue past the first "
        f"trip, got {len(calls)} chat calls"
    )


# ---------- scratchpad-maintenance nudge guard (2026-08-26) ----------
# dispatch.py appends a ONE-TIME instruction to the initial prompt telling
# local-family-dispatched agents to keep .agent_scratchpad.md updated with a
# running PROGRESS: n/m line, but nothing in the step loop ever reinforces
# it again. A weak model drops that single early instruction over a long
# transcript even while still making real edits elsewhere, so
# NET_PROGRESS_MAX_STEPS's own counter (which only tracks "any successful
# mutation") never fires. This guard tracks touches to the scratchpad file
# specifically, and — unlike every other guard here — must NEVER park or
# return early: it only injects a reminder and lets the run continue.

def test_scratchpad_nudge_constants_have_expected_defaults():
    """SCRATCHPAD_NUDGE_STEPS defaults to 15 (env LOCAL_AGENT_SCRATCHPAD_NUDGE_STEPS)
    and SCRATCHPAD_ON defaults to True unless PIPELINE_DECOMPOSE_SCRATCHPAD is
    explicitly set to "off"."""
    assert la.SCRATCHPAD_NUDGE_STEPS == 15
    assert la.SCRATCHPAD_ON is True


def test_scratchpad_nudge_fires_after_threshold_steps_with_no_scratchpad_touch(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    # Large so the other step-drift guards don't also fire and confuse the signal.
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(5)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "[step 3] no scratchpad update in 3 steps; nudging" in out, f"output: {out!r}"

    # calls[i] all alias the SAME underlying transcript list once main()
    # returns (chat() is fed one continuously-mutated list, not a fresh one
    # per call) so any calls[i] reflects the final transcript here.
    messages = calls[-1]
    nudge_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
    ]
    assert len(nudge_indices) == 1, (
        f"expected exactly one scratchpad nudge (threshold=3, no scratchpad "
        f"touch across 5 distinct-file steps), got {len(nudge_indices)}: {messages!r}"
    )

    create_file_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and any(
            tc.get("function", {}).get("name") == "create_file"
            for tc in (m.get("tool_calls") or [])
        )
    ]
    assert len(create_file_indices) >= 4, f"expected at least 4 create_file turns: {messages!r}"

    # Boundary: must not fire on step 0 (last_scratchpad_step=0, step=0,
    # 0-0 < SCRATCHPAD_NUDGE_STEPS) -- the nudge must sit after at least the
    # first 3 create_file turns (steps 0,1,2), not before them.
    assert nudge_indices[0] > create_file_indices[2], (
        f"nudge fired before 3 scratchpad-free steps had elapsed "
        f"(fired too early, e.g. at step 0): {messages!r}"
    )


def test_scratchpad_nudge_resets_when_scratchpad_is_touched(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    # Step 1 touches the scratchpad, resetting its counter's baseline to 1.
    # Steps continue to step 3 -- past the ORIGINAL (baseline-0) threshold of
    # 3 -- then the script ends via "done" before step 4, which is where the
    # reset baseline's own next trip (4 - 1 >= 3) would legitimately fire
    # again. This isolates "did the touch suppress the stale threshold" from
    # "does the guard re-arm periodically" (a separate, expected behavior).
    responses = [
        ("create_file", {"path": "other_0.py", "content": "# x"}),
        ("create_file", {"path": ".agent_scratchpad.md", "content": "PROGRESS: 1/3\n"}),
        ("create_file", {"path": "other_2.py", "content": "# x"}),
        ("done", {"summary": "finished before the next scheduled nudge"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected a clean finish, got rc={rc}\noutput: {out!r}"
    assert "no scratchpad update" not in out, (
        f"the touch at step 1 must suppress the stale step-0-baseline nudge "
        f"that would otherwise fire at step 3; output: {out!r}"
    )
    messages = calls[-1]
    assert not any(
        m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
        for m in messages
    ), f"no nudge message should be present: {messages!r}"


def test_scratchpad_nudge_suppressed_when_scratchpad_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", False)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(6)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "no scratchpad update" not in out, f"output: {out!r}"
    messages = calls[-1]
    assert not any(
        m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
        for m in messages
    ), f"SCRATCHPAD_ON=False must suppress the nudge entirely: {messages!r}"


def test_scratchpad_nudge_never_parks(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    # 8 distinct-file steps with threshold 3 crosses the guard's trip point
    # twice (steps 3 and 6) -- it must re-fire each time (mirroring the
    # net-progress guard's own re-nudge shape) without ever parking.
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(8)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, (
        f"the scratchpad nudge guard alone must never park/terminate the "
        f"run; got rc={rc}\noutput: {out!r}"
    )
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    messages = calls[-1]
    nudge_count = sum(
        1 for m in messages
        if m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
    )
    assert nudge_count >= 2, (
        f"expected the guard to re-fire on both trips (steps 3 and 6), "
        f"got {nudge_count}: {messages!r}"
    )


# ---------- view_file range-aware repetition signature (2026-07-22) ----------
# Reading several DIFFERENT regions of one large file (routine when orienting
# in a multi-hundred-line function) must not share a signature with
# re-reading the SAME region 3x - the guard's per-path-only key made both
# indistinguishable, so a story instructing the model to consult 6 different
# locations in one 2500-line file tripped a false-positive lockout almost
# immediately.

def test_view_file_different_ranges_do_not_trip_repetition_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 1, "line_end": 50}),
        ("view_file", {"path": "big.py", "line_start": 500, "line_end": 550}),
        ("view_file", {"path": "big.py", "line_start": 1000, "line_end": 1050}),
        ("view_file", {"path": "big.py", "line_start": 1500, "line_end": 1550}),
        ("done", {"summary": "oriented"}),
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" not in out, (
        f"4 different regions of one file must not look like repetition; output: {out!r}"
    )


def test_view_file_same_range_three_times_still_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 100, "line_end": 150})
        for _ in range(4)
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"the SAME range 3x must still be caught as repetition; output: {out!r}"
    )


@pytest.mark.parametrize("content,expected_name", [
    ('```json\r\n{"name": "bash", "parameters": {"command": "ls"}}\r\n```', "bash"),
    ('{"name": "done", "arguments": {"summary": "ok"}}', "done"),
    ('[TOOL_CALLS]{"name":"view_file","arguments":{"path":"a"}}', "view_file"),
])
def test_recover_tool_calls_parses_text_formats(content, expected_name):
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == expected_name


def test_recover_tool_calls_repairs_python_triple_quoted_arguments():
    """Weaker local models (observed: Qwen2.5-Coder-14B-4bit on mlx) emit a
    str_replace's multi-line code argument using Python triple-quote syntax
    with literal newlines, which is invalid JSON. The recovery path must
    salvage it so the edit is not silently dropped."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "str_replace",\n'
        '  "arguments": {\n'
        '    "path": "rate_limiter.py",\n'
        '    "old_str": "# TODO",\n'
        '    "new_str": """\n'
        'class TokenBucket:\n'
        '    def __init__(self, capacity):\n'
        '        self.capacity = capacity\n'
        '"""\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "str_replace"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "rate_limiter.py"
    assert args["new_str"].startswith("\nclass TokenBucket:")
    assert "def __init__(self, capacity):" in args["new_str"]


def test_recover_tool_calls_tolerates_raw_newlines_in_json_string():
    """A distinct malformation from the triple-quote case (observed live,
    2026-07-17, Qwen2.5-Coder-14B-4bit on mlx, interval_merge task): the model
    uses ordinary double-quoted JSON string syntax for a create_file's
    multi-line `content` argument, but embeds RAW literal newline bytes
    instead of escaping them as `\\n`. This is invalid per strict JSON (a
    literal control character inside a string is illegal), so a raw
    json.loads rejects it with "Invalid control character" - the
    triple-quote repair does not apply (there are no triple quotes here) so
    the tool call was silently dropped every retry, and the agent looped
    regenerating the same correct-but-unparseable content until it hit the
    wall-clock park with the real fix never landed on disk."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "create_file",\n'
        '  "arguments": {\n'
        '    "path": "intervals.py",\n'
        '    "content": "def merge(x):\n'
        '    return x"\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "create_file"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "intervals.py"
    assert "def merge(x):" in args["content"]
    assert "return x" in args["content"]


def test_recover_tool_calls_returns_none_on_non_toolcall_prose():
    """A repair pass must not manufacture a tool call out of ordinary prose
    (no name/JSON object present) - failing closed keeps the step loop from
    executing a phantom call."""
    assert la.recover_tool_calls("I think the tests pass now, nothing to do.") is None


# ---------------------------------------------------------------------------
# _expected_task_paths / _is_off_task_path - pure helpers for the off-task
# drift guard. These extract file paths named in a task brief (backtick-quoted
# or bold-markdown spans) and decide whether a mutating tool call's target
# matches one of them. A brief that names no files must fail open (never flag).
# ---------------------------------------------------------------------------

def test_expected_task_paths_extracts_backtick_quoted_paths():
    """Backtick-quoted `path/to/file.py` spans are the first naming convention
    agent_instructions use; both named paths must come back, leading './'
    stripped."""
    task = "Edit `pipeline/server.py` and `utils/helpers.py` to add logging."
    assert la._expected_task_paths(task) == {"pipeline/server.py", "utils/helpers.py"}


def test_expected_task_paths_extracts_bold_markdown_paths():
    """Bold-markdown **path/to/file.py** spans are the numbered-list convention
    this repo's real agent_instructions use; both named paths must come back."""
    task = "1. **pipeline/server.py**\n2. **utils/helpers.py**\n3. Run the tests."
    assert la._expected_task_paths(task) == {"pipeline/server.py", "utils/helpers.py"}


def test_expected_task_paths_returns_empty_set_for_no_paths_named():
    """A task string with no path-like tokens yields an empty set - the guard
    must then fail open rather than flag every edit as off-task."""
    assert la._expected_task_paths("Refactor the logging module for clarity.") == set()


def test_expected_task_paths_returns_empty_set_for_empty_task():
    """An empty task string yields an empty set (boundary: empty input)."""
    assert la._expected_task_paths("") == set()


def test_expected_task_paths_strips_leading_dot_slash():
    """A backtick-quoted `./pipeline/server.py` is normalized to
    `pipeline/server.py` so suffix/basename matching is consistent."""
    assert la._expected_task_paths("Edit `./pipeline/server.py`") == {"pipeline/server.py"}


def test_expected_task_paths_returns_a_set():
    """The return type is a set (dedupes repeated mentions); assert the type
    explicitly so a list/tuple return fails loudly."""
    out = la._expected_task_paths("Edit `a.py` then `a.py` again")
    assert isinstance(out, set)
    assert out == {"a.py"}


def test_is_off_task_path_false_for_exact_match():
    """An exact match against an expected path is on-task -> False."""
    assert la._is_off_task_path("pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_dot_slash_prefixed_relative_form():
    """A './'-prefixed relative form of an expected path is still on-task
    (path-suffix containment) -> False."""
    assert la._is_off_task_path("./pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_shared_basename():
    """A differently-rooted file sharing only the basename with an expected
    path is treated as on-task -> False (basename is the reliable signal)."""
    assert la._is_off_task_path("src/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_true_for_unrelated_file():
    """A path sharing no basename or suffix with any expected path is off-task
    -> True."""
    assert la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}) is True


def test_is_off_task_path_fails_open_when_expected_is_empty():
    """A brief that names no files gives the guard nothing to compare against;
    any path against an empty expected set must fail open -> False."""
    assert la._is_off_task_path("scripts/anything.py", set()) is False


def test_is_off_task_path_false_for_empty_path():
    """An empty path string is never flagged -> False regardless of expected."""
    assert la._is_off_task_path("", {"pipeline/server.py"}) is False


def test_is_off_task_path_returns_bool_not_truthy_value():
    """The contract is a real bool (not e.g. None/0/1); assert the type so a
    truthy-but-wrong-typed return fails loudly on both branches."""
    assert la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}) is True
    assert isinstance(la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}), bool)
    assert isinstance(la._is_off_task_path("pipeline/server.py", {"pipeline/server.py"}), bool)


def test_local_agent_main_writes_boot_line_before_first_chat(tmp_path, monkeypatch, capsys):
    """The startup heartbeat in main() must flush to stdout *before* the
    first LLM call. check_story_status relies on this: a 0-byte agent.log
    after dispatch means the process never reached main() (a genuine failed
    launch), while a log with [boot] and no further output means the agent
    is alive and queued on Ollama's -np 1 worker.

    We assert the [boot] line lands first by running main() with chat()
    replaced by a recording fake. After main() returns, the captured stdout
    must start with [boot] pid=... and the recorded chat calls must come
    after that line was emitted."""
    # Run the agent from inside tmp_path so its git/agent.log side effects
    # are isolated to the test, and so the boot line's working directory
    # claim is benign.
    monkeypatch.setattr(la, "CWD", tmp_path)
    # Done-on-first-step: a single valid tool call to `done` with a clean
    # worktree exits immediately after the heartbeat prints.
    chat_calls = []

    def _fake_chat(messages):
        chat_calls.append(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done",
                                            "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert chat_calls, "main() should have invoked chat() at least once"
    # The very first line of output is the heartbeat; the [step 0] line
    # (if any) comes after it. Splitting on the first non-boot newline and
    # asserting boot precedes everything else proves the heartbeat was
    # emitted *before* the LLM round-trip.
    first_line = out.split("\n", 1)[0]
    assert first_line.startswith("[boot] pid="), (
        f"expected [boot] pid= as first stdout line, got: {first_line!r}\n"
        f"full output: {out!r}"
    )
    # The recorded chat() call is the LLM round-trip; the [boot] line is
    # emitted *before* it. We can't time-order the two directly from the
    # captured output, but a missing or empty stdout would be a clear
    # regression: a process whose [boot] line never flushed (e.g. someone
    # removed flush=True) is exactly the bug this heartbeat defends
    # against.


def _sequence_chat(responses):
    """Return a fake chat() that yields a tool-call response from `responses`
    in order. Each entry is a (tool_name, args_dict) tuple. Records every
    call into the returned `calls` list."""
    calls = []

    def _fake(messages):
        calls.append(messages)
        idx = len(calls) - 1
        if idx >= len(responses):
            # ran out of scripted responses — bail to done to end the loop
            fn, args = "done", {"summary": "out of scripted responses"}
        else:
            fn, args = responses[idx]
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": fn, "arguments": args}}]}

    return _fake, calls


def test_local_agent_read_heavy_loop_nudges_once_then_parks(tmp_path, monkeypatch, capsys):
    """Strict/wedging path: when devstral RE-READS an already-seen target
    without ever calling create_file / str_replace, the read-heavy guard
    must fire — one corrective nudge after READ_HEAVY_WINDOW reads, then
    park after another READ_HEAVY_WINDOW if the model ignores the nudge.

    Distinct-target exploration (reading many files once each) is covered
    by test_local_agent_read_heavy_distinct_exploration_reaches_an_edit —
    that is the lenient path. This test pins the strict path: a target
    repeated within the post-nudge window is re-reading (wedging), and
    parks at 2 * READ_HEAVY_WINDOW (default 12).

    We script 6 distinct reads (nudge), then a post-nudge window of 6
    reads where one target repeats. Each target appears <= 2 times total
    so the per-target repetition guard (seen >= 3) does not fire — this
    test is about the read-heavy guard, not the per-target one."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        # 6 distinct reads -> nudge at READ_HEAVY_WINDOW (6).
        ("bash", {"command": "cat a"}),
        ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}),
        ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}),
        ("bash", {"command": "cat f"}),
        # Post-nudge window: 'cat g' repeats once within the window
        # (re-reading a target = wedging, not exploration). Each target
        # is still <= 2 total, so the per-target guard (seen >= 3) stays
        # out of the way.
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}),
        ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}),
        ("bash", {"command": "cat k"}),
        # Spare reads in case of an off-by-one; the park fires at call 12.
        ("bash", {"command": "cat spare1"}),
        ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()

    out = capsys.readouterr().out
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    # The nudge fires once, the park fires once.
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert out.count("[parking: read-heavy after nudge]") == 1, f"expected 1 park, output: {out!r}"
    # chat() called at most 12 times — definitely not the full 40 step cap.
    # The exact count depends on the iteration between nudge and park, but
    # the upper bound is 2 * READ_HEAVY_WINDOW (default 12).
    assert len(calls) <= 12, (
        f"guard should stop the run well before the 40-step cap, "
        f"got {len(calls)} chat calls"
    )


# ---------------------------------------------------------------------------
# Off-task-drift guard (Mode 31) wiring tests.
#
# These drive the real la.main() entrypoint end-to-end with chat() mocked at
# its true external boundary (the _sequence_chat fake), exactly like the
# read-heavy/repetition-guard tests above. The helpers _expected_task_paths
# and _is_off_task_path already exist in scripts/local_agent.py (added by a
# prior story); these tests verify they are actually WIRED INTO main()'s
# step loop — a single off-task mutating edit nudges once and does not park,
# a second DIFFERENT off-task edit after the nudge parks (return 3), editing
# only files named in the brief never nudges, and a brief naming no files at
# all fails open (never nudges).
# ---------------------------------------------------------------------------

_OFF_TASK_BRIEF = "Refactor `pipeline/server.py` to add a health-check endpoint."


def test_off_task_edit_nudges_once_and_does_not_park_on_a_single_file(
    tmp_path, monkeypatch, capsys
):
    """A single off-task mutating edit prints exactly one `[off-task nudge:`
    line and does NOT park (rc == 0). A single stray file must not terminate
    the run — the guard nudges once and lets the agent explain itself."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, (
        f"expected exactly one off-task nudge, output: {out!r}"
    )
    assert rc == 0, f"a single stray file must not park; got rc={rc}\noutput: {out!r}"
    assert "[parking: off-task drift" not in out, (
        f"a single off-task edit must not park; output: {out!r}"
    )


def test_off_task_edits_on_two_distinct_paths_park_after_the_nudge(
    tmp_path, monkeypatch, capsys
):
    """Two off-task mutating edits on two DIFFERENT unrelated paths: the first
    nudges, the second (a distinct target after the nudge) parks the run with
    exit code 3. This is the Mode 31 failure mode — a dispatched agent that
    abandons its task and drifts onto unrelated files."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("create_file", {"path": "elsewhere/other.py", "content": "y = 2\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, (
        f"expected exactly one nudge (only the first distinct target nudges), "
        f"output: {out!r}"
    )
    assert "[parking: off-task drift" in out, (
        f"expected a parking line for the second distinct off-task target, "
        f"output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got rc={rc}\noutput: {out!r}"


def test_on_task_edits_never_nudge(tmp_path, monkeypatch, capsys):
    """Editing a file named in the brief must never trip the off-task guard.
    Create the named file with real content, then a str_replace that actually
    matches and edits it — no nudge, clean finish (rc == 0)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    # Pre-create the on-task file so str_replace has something to match.
    (tmp_path / "pipeline" / "server.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "server.py").write_text("OLD = 1\n")
    responses = [
        ("str_replace", {
            "path": "pipeline/server.py",
            "old_str": "OLD = 1",
            "new_str": "NEW = 2",
        }),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, (
        f"on-task edit must never nudge; output: {out!r}"
    )
    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"


def test_brief_naming_no_paths_never_nudges(tmp_path, monkeypatch, capsys):
    """A brief that names no files at all (plain prose, no backtick/bold paths)
    must fail open: the off-task guard never nudges, because there is nothing
    reliable to compare against. Editing any path is allowed."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Make the service more robust and add tests.")
    responses = [
        ("create_file", {"path": "any/random/path.py", "content": "z = 3\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, (
        f"a brief naming no paths must fail open and never nudge; output: {out!r}"
    )
    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"


def test_local_agent_park_disabled_continues_past_strict_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False is the kill-switch for a capable model that re-reads
    aggressively (e.g. minimax-m3:cloud re-viewing a file before editing): the
    guards still nudge but never terminate (return 3), so the step cap becomes
    the only bound and the run keeps its full budget to reach a first edit.

    Same read-heavy-repetition scenario as
    test_local_agent_read_heavy_loop_nudges_once_then_parks, which parks at
    call 12 (rc=3) with PARK_ENABLED at its default. With PARK_ENABLED=False
    the park message still prints (the detection is unchanged) but the run
    must NOT terminate there — it progresses well past call 12."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = [
        ("bash", {"command": "cat a"}), ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}), ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}), ("bash", {"command": "cat f"}),
        # Post-nudge window with a repeat (cat g twice) -> strict park signal.
        ("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}), ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # The detection still fires — nudge and park message both print — but the
    # run does NOT exit 3; it continues until the scripted responses run out
    # (_sequence_chat then yields `done`, rc=0).
    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"nudge must still fire, output: {out!r}"
    assert "[parking: read-heavy after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    # The default-PARK test parks at <= 12 calls; disabled parking must
    # progress past that point (the whole point of the switch).
    assert len(calls) > 12, (
        f"disabled parking should let the run continue past the strict-park "
        f"point, got {len(calls)} chat calls"
    )


def test_local_agent_park_disabled_continues_past_per_target_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False on the per-target repetition site (the third park
    site, distinct from the read-heavy one above): re-viewing the same file
    3x nudges, a 4th would park (return 3) by default. With parking disabled
    the run must continue past that point. Covers the site the
    read-heavy-repetition test doesn't; the distinct-windows site is
    mechanically identical (same `if not PARK_ENABLED: recent_tools.clear();
    break` shape) and verified by reading."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    # view_file the same path repeatedly -> per-target seen >= 3 nudges, then
    # parks on the next. Not enough reads to trip the read-heavy window (6),
    # so this isolates the per-target guard.
    responses = [("view_file", {"path": "static/style.css"}) for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" in out, f"per-target nudge must still fire, output: {out!r}"
    assert "[parking: repeated action after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    # Default parks at the 4th same-target call; disabled parking continues
    # past it (the run only ends when scripted responses exhaust -> done).
    assert len(calls) > 4, (
        f"disabled parking should let the run continue past the per-target "
        f"park point, got {len(calls)} chat calls"
    )


def test_local_agent_repeated_per_target_park_never_leaves_tool_call_unanswered(
    tmp_path, monkeypatch, capsys,
):
    """Bug found live 2026-07-22 (Mode 33, MODE-29-REVIEW-STORY-LOCK-GUARD):
    with PARK_ENABLED=False (the scheduler plist), only the FIRST trip of the
    per-target repetition guard appended anything to the conversation (the
    nudge, as a `user`-role message). Every trip after that for the rest of
    the run silently dropped the tool call -- `break` with nothing appended
    -- leaving the triggering assistant message's `tool_calls` entry with no
    `tool`-role answer at all. Inspecting the live `.agent_transcript.json`
    confirmed this directly: two consecutive `assistant` messages with no
    intervening `tool` message, appearing immediately before gpt-oss:20b's
    Harmony-format output started leaking raw special tokens
    (`<|start|>assistant<|channel|>...`) and the run eventually collapsed
    into narration-only turns and parked.

    Fix: every trip of the guard, not just the first, must append a
    `tool`-role response for the triggering call -- this keeps the
    transcript well-formed (no orphaned tool_calls) AND re-delivers the
    corrective guidance every time instead of going silent after one
    warning."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    # Same script as test_local_agent_park_disabled_continues_past_per_target_park:
    # 6 identical view_file calls -> seen=1,2 pass through normally, seen=3..6
    # each trip the guard (first trip nudges, the other 3 previously parked
    # silently).
    responses = [("view_file", {"path": "static/style.css"}) for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

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

    # 4 trips of the guard fire (seen reaches 3, 4, 5, 6) -- each must
    # re-deliver the corrective guidance, not just the first.
    nudge_msgs = [
        m for m in messages
        if m.get("role") == "tool" and "STOP reading" in (m.get("content") or "")
    ]
    assert len(nudge_msgs) == 4, (
        f"expected the corrective guidance re-delivered on every one of the "
        f"4 guard trips, got {len(nudge_msgs)}: {messages!r}"
    )


def test_local_agent_read_heavy_park_disabled_renudges_every_window(tmp_path, monkeypatch, capsys):
    """Same bug class as test_local_agent_repeated_per_target_park_never_leaves_tool_call_unanswered
    (Mode 33), applied to the read-heavy guard's `has_repetition` branch:
    with PARK_ENABLED=False, only the first read-heavy window that trips
    the guard got a corrective message; every later window that also showed
    repetition went completely silent (a bare `break`). This guard doesn't
    orphan a tool_calls entry (the real tool response for the triggering
    call already landed before this check runs), but it shares the "nudge
    once, then silence for the rest of the run" defect. Fix: append a fresh
    corrective message on every window that trips, not just the first.

    Three windows of 6 non-mutating calls: window 1 is all-distinct (fires
    the initial nudge), windows 2 and 3 each repeat one target within the
    window (wedging, not exploration) -> the has_repetition branch should
    fire twice more, each time appending the renewed guidance."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = (
        [("bash", {"command": f"cat {c}"}) for c in "abcdef"]
        + [("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
           ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
           ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"})]
        + [("bash", {"command": "cat l"}), ("bash", {"command": "cat l"}),
           ("bash", {"command": "cat m"}), ("bash", {"command": "cat n"}),
           ("bash", {"command": "cat o"}), ("bash", {"command": "cat p"})]
    )
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    capsys.readouterr()

    messages = calls[-1]
    renudge_msgs = [
        m for m in messages
        if m.get("role") == "user" and "still re-reading targets" in (m.get("content") or "")
    ]
    assert len(renudge_msgs) == 2, (
        f"expected the read-heavy re-nudge on both post-initial-nudge "
        f"windows that showed repetition, got {len(renudge_msgs)}: {messages!r}"
    )


def test_local_agent_read_heavy_distinct_exploration_reaches_an_edit(tmp_path, monkeypatch, capsys):
    """Lenient path: a multi-file bug fix legitimately reads many DISTINCT
    targets (each file once) before its first edit. The exploration-aware
    read-heavy guard must NOT park such a run at 2 * READ_HEAVY_WINDOW —
    it nudges once (pushing the agent to act) then lets all-distinct
    exploration continue up to a bounded cap, so a run that reaches an
    edit finishes cleanly.

    Pre-fix (flat 12-read cutoff) this parked at step 12 before the
    create_file, returning 3. Post-fix it reaches the edit and exits 0.
    This is the regression that parked both pipeline-fix agents
    (3e44b5a7, 900c765b) during fresh bug-fix exploration on 2026-06-28."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(16)]
    # 16 distinct reads -> nudge at 6, then all-distinct post-nudge windows
    # (leniency), never reaching the 24-read distinct cap. A mutating call
    # then resets the streak and the run finishes.
    responses.append(("create_file", {"path": "new_module.rs", "content": "// real code\n"}))
    responses.append(("done", {"summary": "wrote the module"}))
    responses.append(("done", {"summary": "wrote the module"}))
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"distinct exploration that reaches an edit must finish, got rc={rc}\noutput: {out!r}"
    )
    assert "[parking: read-heavy" not in out, (
        f"all-distinct exploration must not park before the bounded cap; output: {out!r}"
    )


def test_local_agent_read_heavy_distinct_exploration_is_bounded(tmp_path, monkeypatch, capsys):
    """The leniency for distinct exploration is BOUNDED, not disabled: a
    run that keeps reading distinct targets with NO eventual mutation is
    still wedging, just a slower kind. It must park after
    READ_HEAVY_WINDOW + READ_HEAVY_DISTINCT_WINDOWS * READ_HEAVY_WINDOW
    reads (6 + 3*6 = 24 by default) — not the flat 12, but still bounded.

    Pre-fix this parked at 12 (flat cutoff). Post-fix it parks at ~24 with
    a distinct-windows park message. Asserting the call count is > 12 pins
    the leniency; asserting it is <= 24 pins the bound."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(30)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 3, f"unbounded distinct reading must still park, got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert "distinct windows" in out, (
        f"distinct-cap park must log a distinct-windows message; output: {out!r}"
    )
    # Leniency: parks later than the flat 12. Bound: no later than 24.
    assert len(calls) > 12, (
        f"distinct exploration must get more than the flat 12 reads; got {len(calls)}"
    )
    assert len(calls) <= 24, (
        f"distinct exploration must be bounded (<= 24 reads); got {len(calls)}"
    )


def test_local_agent_read_heavy_distinct_constants():
    """The exploration-aware guard adds a bounded-leniency constant. It
    must be defined and the nudge threshold stays 6 so the early 'push to
    act' nudge is preserved (the leniency only relaxes the PARK, not the
    nudge)."""
    assert la.READ_HEAVY_WINDOW == 6
    assert hasattr(la, "READ_HEAVY_DISTINCT_WINDOWS")
    assert la.READ_HEAVY_DISTINCT_WINDOWS == 3


def test_local_agent_does_not_nudge_with_regular_writes(tmp_path, monkeypatch, capsys):
    """The sliding window only fires when the LAST READ_HEAVY_WINDOW calls
    are all non-mutating. A model that interleaves one write per window
    (e.g. reads 5, writes 1, reads 5, writes 1, ...) must NOT be flagged —
    it's making forward progress, just slowly.

    Sequence: read, read, read, read, read, create_file, repeated. The
    write sits in the deque so the all-non-mutating check never holds.

    Each bash command is unique to dodge the per-target repetition guard,
    which is testing a different signal and isn't what this test is about."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "echo 1 > /dev/null"}),
        ("view_file", {"path": "fake.py"}),
        ("bash", {"command": "echo 2 > /dev/null"}),
        ("view_file", {"path": "fake2.py"}),
        ("bash", {"command": "echo 3 > /dev/null"}),
        # write in the middle — keeps the deque mixed.
        ("create_file", {"path": "new_module.rs", "content": "// real code\n"}),
        # Continue the pattern. Deque after step 9: [bash, view_file, bash,
        # view_file, bash, create_file] — one mutating in the window, so
        # the all-non-mutating check fails.
        ("bash", {"command": "echo 4 > /dev/null"}),
        ("view_file", {"path": "fake3.py"}),
        ("bash", {"command": "echo 5 > /dev/null"}),
        ("view_file", {"path": "fake4.py"}),
        ("bash", {"command": "echo 6 > /dev/null"}),
        # done once rejected, then auto-WIP-commits and accepts.
        ("done", {"summary": "wrote the module"}),
        ("done", {"summary": "wrote the module"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()

    out = capsys.readouterr().out
    assert "[read-heavy nudge:" not in out, (
        f"nudge must NOT fire when at least one mutating call sits in "
        f"the sliding window; output: {out!r}"
    )
    # After 1 done rejection, the harness auto-WIP-commits and accepts.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_local_agent_read_heavy_park_does_not_wip_commit_when_clean(tmp_path, monkeypatch, capsys):
    """When the read-heavy guard parks the run on the strict (re-reading)
    path, it should not spuriously WIP-commit if the worktree is clean.
    (A read-only run by definition hasn't edited any files, so
    worktree_dirty() is False — auto_wip_commit is a no-op, but the branch
    must be reachable.) Uses the same re-reading scenario as
    test_local_agent_read_heavy_loop_nudges_once_then_parks so the strict
    park fires; the reads don't create files so the worktree stays clean."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "cat a"}),
        ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}),
        ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}),
        ("bash", {"command": "cat f"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}),
        ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}),
        ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}),
        ("bash", {"command": "cat spare2"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)
    # Sanity: tmp_path is empty, so worktree_dirty() returns False.
    assert not la.worktree_dirty()

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 3
    assert "[parking: read-heavy after nudge]" in out
    # No spurious commit messages in the log.
    assert "WIP" not in out, f"no commit should be made on a clean worktree; output: {out!r}"


def test_local_agent_str_replace_repetitions_do_not_fire_per_target_guard(
    tmp_path, monkeypatch, capsys,
):
    """Fix B: str_replace calls to the same path are NOT counted by the
    per-target repetition guard, because each one produces a different file
    state and the next edit's old_str would differ (or run_tool would
    reject it as 'not found'). A model iterating to fix build errors is
    making forward progress, not repeating.

    Without Fix B this script would park at the 4th str_replace (3rd
    repetition), killing the agent mid-fix (which is exactly what killed
    421b308b in the post-PR #30 rerun). With Fix B the agent runs to done
    and exits cleanly."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        # Three non-mutating calls (no per-target trip — all unique).
        ("bash", {"command": "echo a > /dev/null"}),
        ("bash", {"command": "echo b > /dev/null"}),
        ("bash", {"command": "echo c > /dev/null"}),
        # Six str_replace calls to the SAME path. Pre-fix code would
        # park at the 4th. Post-fix code ignores str_replace in the
        # per-target counter.
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub",
            "new_str": "// stub 1",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 1",
            "new_str": "// stub 2",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 2",
            "new_str": "// stub 3",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 3",
            "new_str": "// stub 4",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 4",
            "new_str": "// stub 5",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 5",
            "new_str": "// stub 6",
        }),
        # Done once rejected because worktree is dirty, auto-WIP commits
        # on second attempt.
        ("done", {"summary": "implemented"}),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # Per-target repetition nudge must NOT fire for str_replace calls.
    assert "[repetition nudge]" not in out, (
        f"per-target guard should ignore str_replace repetitions; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"per-target guard should not park on str_replace; output: {out!r}"
    )
    # After 1 done rejection, the harness auto-WIP-commits and accepts.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_local_agent_repeated_create_file_on_existing_path_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """The per-target repetition guard must catch a model stuck resubmitting
    create_file against a path that already exists (non-destructive-editor
    rejects each one with "already exists" - it should use str_replace
    instead). Observed live 2026-07-15: create_file's own membership in
    MUTATING_TOOLS made `if fn in MUTATING_TOOLS: seen.clear()` wipe out its
    OWN signature's count on every single call, so seen[sig] could never
    accumulate past 1 - the guard was permanently inert for this exact
    pattern, and a real trial burned 34 consecutive create_file calls (its
    entire step budget) with no nudge ever firing."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [("create_file", {"path": "lru_cache.py", "content": "class LRUCache:\n    x = 1\n"})
                 for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"repeated create_file on an existing path must trip the per-target "
        f"guard; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" in out, (
        f"ignoring the nudge must park the run; output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) <= 6, (
        f"guard should stop well before burning the whole scripted sequence, "
        f"got {len(calls)} chat calls"
    )


def test_local_agent_interleaved_failed_mutations_still_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Reproduces the real 2026-07-15 lru_cache incident precisely: a model
    alternates create_file (rejected: already exists) with str_replace
    (rejected: old_str not found) and the occasional successful bash check,
    never making real progress. Pre-fix, `if fn in MUTATING_TOOLS:
    seen.clear()` ran on every mutating call REGARDLESS OF OUTCOME, so each
    failed str_replace wiped out create_file's accumulating failure count
    before it could ever reach the threshold - the guard was inert for this
    exact interleaved pattern (34 consecutive calls burned live with zero
    nudges). The fix must gate clearing on the mutation actually SUCCEEDING,
    not merely being attempted, so failed str_replace/bash calls in between
    do not reset create_file's accumulating failure count."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [
        ("create_file", {"path": "lru_cache.py", "content": "content1"}),  # fails: exists
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE", "new_str": "x"}),  # fails: not found
        ("bash", {"command": "true"}),  # succeeds, not a mutating tool
        ("create_file", {"path": "lru_cache.py", "content": "content2"}),  # fails: exists
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE2", "new_str": "x"}),  # fails: not found
        ("bash", {"command": "true"}),  # succeeds
        ("create_file", {"path": "lru_cache.py", "content": "content3"}),  # 3rd failure -> nudge
        ("create_file", {"path": "lru_cache.py", "content": "content4"}),  # ignored nudge -> park
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"interleaved failed mutations must still trip the per-target "
        f"guard; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" in out, (
        f"ignoring the nudge must park the run; output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) <= 8, (
        f"guard should stop well before burning the whole scripted sequence, "
        f"got {len(calls)} chat calls"
    )


def test_local_agent_edit_between_reads_resets_per_target_repetition_counter(
    tmp_path, monkeypatch, capsys,
):
    """The per-target repetition guard's counter must not be a lifetime
    cumulative count of how many times a path was EVER viewed in the run — it
    must reset on real progress (a str_replace/create_file edit), since a
    model re-checking a file it just edited is not "repeating with no
    progress" just because it also happened to view that same path earlier.

    Without this fix, view_file(X), view_file(X), str_replace(X),
    view_file(X), view_file(X) hits the per-target threshold (seen >= 3) on
    the second post-edit view, purely from the pre-edit reads still counting
    toward the same lifetime total — nudging and then parking a run that is
    actually making progress. This reproduces the 2026-07-04 gpt-oss
    false-positive parks on ratelimiter_inspect's RLI-2 (both trials parked
    re-viewing rate_limiter.py a 3rd/4th time across the whole run, with real
    edits and test runs in between each view)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "rate_limiter.py").write_text("class TokenBucket:\n    pass\n")
    responses = [
        ("view_file", {"path": "rate_limiter.py"}),
        ("view_file", {"path": "rate_limiter.py"}),
        ("str_replace", {
            "path": "rate_limiter.py",
            "old_str": "class TokenBucket:\n    pass\n",
            "new_str": "class TokenBucket:\n    def __init__(self):\n        pass\n",
        }),
        ("view_file", {"path": "rate_limiter.py"}),
        ("view_file", {"path": "rate_limiter.py"}),
        ("done", {"summary": "implemented"}),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"a real edit between reads must reset the per-target counter; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"a real edit between reads must not lead to a park; output: {out!r}"
    )
    # After 1 done rejection (worktree dirty from the str_replace), the
    # harness auto-WIP-commits and accepts, same as the str_replace test above.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


# ---------- chat() streaming + retry (2026-06-28 timeout incident) ----------
# Mirrors the oracle-harness tests: a single transient Ollama stall must not
# kill the run. chat() streams and retries. The base harness got the same
# rewrite as the oracle, so we pin its retry contract here too.

class _FakeResp:
    def __init__(self, code):
        self.status_code = code


def _status_error(code):
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=httpx.Request("POST", "http://localhost"),
        response=_FakeResp(code),
    )


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _status_error(self.status_code)

    def iter_lines(self):
        yield from self._lines


class _FakeStreamCM:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *a):
        return False


def test_chat_retries_on_timeout_then_succeeds(monkeypatch):
    """A transient read timeout must not kill the run (2026-06-28 incident:
    all 3 e2e agents died at "LLM call failed: timed out" mid-iteration)."""
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("read timed out")
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(la, "_stream_one_turn", _flaky)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3
    assert msg["content"] == "done"


def test_chat_does_not_retry_on_4xx(monkeypatch):
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(payload):
        calls["n"] += 1
        raise _status_error(404)

    monkeypatch.setattr(la, "_stream_one_turn", _bad_request)
    try:
        la.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(404)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


# ---------- main() trims and retries once on a persistent 5xx (2026-07-22) ----------
# Found live on MODE-29-REVIEW-STORY-LOCK-GUARD: chat()'s own CHAT_MAX_ATTEMPTS
# retry sends the IDENTICAL payload every attempt, so a 5xx caused by an
# oversized transcript (Ollama/llama.cpp returns 500 rather than a clean 4xx
# for a context-window overflow) fails identically every time - confirmed via
# the real failing transcript, ~190K chars / ~47.6K estimated tokens against a
# 32768-token context window. Retrying alone can never help; main() must
# shrink the request. These pin main()'s new recovery: on a 5xx that survives
# chat()'s own retries, trim the transcript (reusing _trim_resumed_transcript)
# and retry chat() exactly once more before giving up.

def test_local_agent_trims_transcript_and_retries_once_on_persistent_5xx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    # A tiny NUM_CTX means genuine post-head content (grown by the two
    # successful reads below) already exceeds the trim budget, so trimming
    # reliably triggers without needing to hand-construct a huge transcript.
    # _trim_resumed_transcript always preserves messages[:2] (system+task) as
    # the head and only ever drops content BEYOND it, so the failing call
    # must not be the very first one - there must be real history to trim.
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected the trim-and-retry to recover, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 4, (
        f"expected exactly 4 chat() calls (2 reads, fail once, succeed on "
        f"the trim-retry), got {calls['n']}\noutput: {out!r}"
    )
    assert "escalating trim and retrying" in out, f"expected the escalation trim log line, output: {out!r}"


def test_local_agent_gives_up_when_trim_retry_also_fails(tmp_path, monkeypatch, capsys):
    """Escalation is bounded, not an open-ended loop: if chat() still fails
    after every escalation round, main() must give up (return 1) rather
    than retrying indefinitely.

    Call count relaxed from 4 to the bounded round count on 2026-08-07:
    an unshrinkable payload now retries unchanged after a backoff instead
    of bailing on the first round (see recover_from_oversized_5xx). The
    property under test - terminates, returns 1 - is unchanged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        raise _status_error(500)

    monkeypatch.setattr(la, "chat", _fake_chat)
    monkeypatch.setattr(la.time, "sleep", lambda _s: None)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected give-up after the trim-retry also fails, got rc={rc}\noutput: {out!r}"
    assert 4 <= calls["n"] <= 6, (
        f"expected the 2 reads plus a bounded escalation (<=3 rounds), "
        f"got {calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed after trim-retry" in out, f"output: {out!r}"


def test_local_agent_does_not_trim_on_4xx(tmp_path, monkeypatch, capsys):
    """A 4xx is a bad request, not a context-overflow signature - trimming
    and retrying would just mask a real bug in the request shape. Must fail
    immediately, same as before this fix, with no trim attempt."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(la, "chat", _bad_request)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected immediate give-up on a 4xx, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 1, f"a 4xx must not trigger a trim-retry, got {calls['n']} calls"
    assert "trimming and retrying" not in out, f"output: {out!r}"


# ---------- 5xx escalation must not crash the agent loop (2026-07-30) ----------
# recover_from_oversized_5xx only catches httpx.HTTPStatusError, so a 4xx it
# re-raises or a non-HTTP backend failure (TransportError, RateLimitedError)
# propagates out of the helper. It is called from inside main()'s
# except-HTTPStatusError handler, and a sibling except-Exception does NOT catch
# exceptions raised from within another except body - so without the guard
# restored at the call site such a failure escapes main() and kills the run,
# regressing the original "must not crash the agent loop" invariant.

def test_local_agent_does_not_crash_on_transport_error_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A TransportError raised by chat() during an escalation round must give up
    (return 1), not propagate out of main() and crash the agent loop."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise httpx.TransportError("connection reset")  # escalation round failure

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a TransportError during escalation, got "
        f"rc={rc}\noutput: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


def test_local_agent_does_not_crash_on_4xx_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A 4xx re-raised by the escalation helper must give up (return 1), not
    escape main() and crash the agent loop - matching the original trim-retry
    path, which caught every failure and returned 1."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise _status_error(400)  # escalation round re-raises a 4xx

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a 4xx during escalation, got rc={rc}\n"
        f"output: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


# ---------- chat() provider routing (LOCAL_AGENT_PROVIDER, S3) ----------
# Ollama (PROVIDER == "ollama", the default) keeps the streaming
# _stream_one_turn path untouched. Any other provider (lmstudio, mlx) goes
# through the blocking _provider_chat_turn seam instead, which delegates to
# inference_providers.get_local_provider().chat(). These tests pin that
# branch and its retry contract without a real LM Studio/MLX server.

def test_chat_default_provider_is_ollama():
    assert la.PROVIDER == "ollama"


def test_chat_uses_stream_one_turn_when_provider_is_ollama(monkeypatch):
    """The default (ollama) branch must never touch the provider seam."""
    monkeypatch.setattr(la, "PROVIDER", "ollama")

    def _boom(messages):
        raise AssertionError("_provider_chat_turn must not be called for ollama")

    monkeypatch.setattr(la, "_provider_chat_turn", _boom)
    monkeypatch.setattr(
        la, "_stream_one_turn",
        lambda payload: {"role": "assistant", "content": "ok"},
    )
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"


def test_chat_routes_through_provider_when_not_ollama(monkeypatch):
    """LOCAL_AGENT_PROVIDER=lmstudio (or mlx) must skip Ollama's streaming
    /api/chat path entirely and use the provider's blocking chat() instead."""
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")

    def _boom(payload):
        raise AssertionError("_stream_one_turn must not be called for lmstudio")

    monkeypatch.setattr(la, "_stream_one_turn", _boom)
    monkeypatch.setattr(
        la, "_provider_chat_turn",
        lambda messages: {"role": "assistant", "content": "from lmstudio",
                           "tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]},
    )
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "from lmstudio"
    assert msg["tool_calls"][0]["function"]["name"] == "done"


def test_provider_chat_turn_extracts_message_from_envelope(monkeypatch):
    """_provider_chat_turn must return just the message dict (matching
    _stream_one_turn's return contract), not the full provider envelope."""
    captured = {}

    class _FakeProvider:
        def chat(self, messages, *, model, num_ctx, temperature, tools, endpoint, timeout):
            captured.update(model=model, num_ctx=num_ctx, temperature=temperature,
                             tools=tools, endpoint=endpoint, timeout=timeout)
            return {"message": {"role": "assistant", "content": "hi"},
                    "prompt_eval_count": 3, "eval_count": 5}

    monkeypatch.setattr(la.inference_providers, "get_local_provider", lambda: _FakeProvider())
    msg = la._provider_chat_turn([{"role": "user", "content": "hi"}])
    assert msg == {"role": "assistant", "content": "hi"}
    assert captured["model"] == la.MODEL
    assert captured["tools"] == la.TOOLS


def test_chat_retries_on_provider_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(la, "_provider_chat_turn", _flaky_5xx)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


def test_chat_does_not_retry_on_provider_4xx(monkeypatch):
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(la, "_provider_chat_turn", _bad_request)
    try:
        la.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(400)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


def test_chat_retries_on_provider_rate_limited_error_then_succeeds(monkeypatch):
    """A 429 from the local server (RateLimitedError) is transient — chat()
    must retry it like a 5xx, not treat it as a terminal failure."""
    monkeypatch.setattr(la, "PROVIDER", "mlx")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _rate_limited_then_ok(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise la.inference_providers.RateLimitedError("429")
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(la, "_provider_chat_turn", _rate_limited_then_ok)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


# ---------- Wall-clock timeout ----------

def test_main_exits_with_wip_commit_when_wall_clock_exceeded(tmp_path, monkeypatch):
    """When wall-clock timeout is exceeded between steps, main() must auto-WIP-commit and return 2."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "TIMEOUT", 0.0)  # expire immediately
    monkeypatch.setattr(la, "MAX_STEPS", 100)

    # Create a dirty worktree file so auto-WIP-commit has something to commit.
    (tmp_path / ".git").mkdir()
    (tmp_path / "work.txt").write_text("in progress")

    git_calls = []

    def fake_git_run(cmd, **kwargs):
        git_calls.append(cmd)
        class R:
            returncode = 0
            stdout = "mocked"
            stderr = ""
        return R()

    def fake_chat(messages):
        # A valid tool call reply so the loop advances at least one step.
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "done", "arguments": {"result": "x"}}}],
        }

    monkeypatch.setattr(la.subprocess, "run", fake_git_run)
    monkeypatch.setattr(la, "chat", fake_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: git_calls.append(["wip", reason]))

    # patch time.monotonic so the first check already sees elapsed > TIMEOUT
    call_count = [0]
    def fake_monotonic():
        call_count[0] += 1
        if call_count[0] == 1:
            return 0.0   # start time
        return 1000.0    # way past timeout
    monkeypatch.setattr(la.time, "monotonic", fake_monotonic)

    result = la.main()
    assert result == 2, "wall-clock timeout should exit with code 2 (same as step cap)"
    assert any("wip" in str(c) for c in git_calls), "auto-WIP-commit should run on timeout"


def test_stream_one_turn_assembles_streamed_chunks(monkeypatch):
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."


# ---------- reasoning-model 'thinking' field (gpt-oss:20b onboarding) ----------
# gpt-oss:20b streams its chain-of-thought in a separate `thinking` field on
# each chunk, distinct from `content`. If a turn's `content` is empty on every
# chunk (the model only "thought" and never emitted a final answer/tool call
# as content), the assembled message would otherwise have empty content —
# starving recover_tool_calls() of anything to parse. `thinking` must be used
# ONLY as a content fallback for a turn with zero real content; it must never
# be appended to genuine content (that would leak raw reasoning traces into
# tool-call parsing, commit messages, and logs).

def test_stream_one_turn_falls_back_to_thinking_when_content_entirely_empty(monkeypatch):
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "Let me "}}),
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "think about this."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["content"] == "Let me think about this."


def test_stream_one_turn_does_not_leak_thinking_into_real_content(monkeypatch):
    """When content IS present anywhere in the turn, thinking fragments (even
    ones interleaved on the same chunks) must never be appended to it."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "pondering "}}),
        json.dumps({"message": {"role": "assistant", "content": "Hello ", "thinking": "more thoughts "}}),
        json.dumps({"message": {"role": "assistant", "content": "world.", "thinking": ""}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["content"] == "Hello world."


def test_stream_one_turn_native_tool_call_with_thinking_and_empty_content(monkeypatch):
    """Native tool_calls plus thinking-only turns (no real content) must still
    capture tool_calls correctly, and the thinking-fallback must not interfere
    with or duplicate the tool call."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "figuring out the call "}}),
        json.dumps({
            "message": {
                "role": "assistant", "content": "", "thinking": "now calling.",
                "tool_calls": [{"function": {"name": "bash", "arguments": {"command": "ls"}}}],
            },
        }),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["tool_calls"] == [{"function": {"name": "bash", "arguments": {"command": "ls"}}}]
    assert msg["content"] == "figuring out the call now calling."


def test_stream_one_turn_no_thinking_key_is_noop(monkeypatch):
    """Existing-behavior regression: a plain devstral-style stream (text-only
    content, no `thinking` key at all in any chunk) must assemble identically
    to today's behavior — the absence of `thinking` must be a no-op."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."
    assert "tool_calls" not in msg


def test_stream_one_turn_uses_configurable_connect_timeout(monkeypatch):
    """connect=10.0 was hardcoded and too short for a cold local-model load -
    observed live 2026-07-13: both glm-4.7-flash (~17s cold load) and
    qwen3-coder:30b timed out identically. Ollama aborts the in-flight load
    and frees the memory it had claimed the instant the client gives up, so
    a too-short connect timeout causes an infinite load/abort/retry cycle
    that never completes rather than a genuine failure. Mirrors the same
    fix in local_agent_oracle.py (CONNECT_TIMEOUT_SECONDS)."""
    monkeypatch.setattr(la, "CONNECT_TIMEOUT_SECONDS", 45.0)
    captured = {}

    def _fake_stream(method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert captured["timeout"].connect == 45.0


# ---------- live chars/token calibration from ollama's own prompt_eval_count
# (2026-07-29) ----------
# _CHARS_PER_TOKEN_ESTIMATE=4 is a guess with no tokenizer behind it. Live on
# the ollama server log: a real 41,921-token gpt-oss prompt measured ~2.35
# chars/token against a budget computed from the fixed 4.0 guess - the
# budget was already ~28% over NUM_CTX by the time a trim would fire. Ollama
# reports the real prompt token count for every turn in the streamed done
# chunk's `prompt_eval_count`; use it to replace the guess with a live ratio.

def test_stream_one_turn_captures_prompt_eval_count_and_calibrates_ratio(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    payload = {"model": "x", "messages": [{"role": "user", "content": "x" * 250}],
               "tools": [], "stream": True}
    la._stream_one_turn(payload)
    assert la._last_prompt_eval_count == 100
    # 250 message chars + len("[]") for the empty tools schema, over 100
    # measured prompt tokens (see the tools-schema test below).
    assert la._measured_chars_per_token == pytest.approx((250 + 2) / 100)


def test_stream_one_turn_leaves_calibration_unset_without_prompt_eval_count(monkeypatch):
    """A done chunk that never reports a count (any non-ollama backend that
    happened to route here, or a truncated stream) must not crash and must
    not fabricate a calibration - the caller falls back to the fixed guess."""
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert la._last_prompt_eval_count is None
    assert la._measured_chars_per_token is None


def test_stream_one_turn_counts_tools_schema_in_calibration(monkeypatch):
    """prompt_eval_count covers the WHOLE prompt - messages plus the tools
    schema plus chat-template scaffolding - so calibrating against message
    chars alone biases the ratio badly low early in a run, when the ~3.4KB
    tools schema dominates a still-small transcript. Measured: a first turn
    calibrated to 0.67 instead of ~2.35, shrinking the reactive-5xx trim
    budget ~3.5x more than needed. The tools payload must be counted."""
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    tools = [{"type": "function", "function": {"name": "x" * 146}}]
    tools_chars = len(json.dumps(tools))
    payload = {"model": "x", "messages": [{"role": "user", "content": "y" * 250}],
               "tools": tools, "stream": True}
    la._stream_one_turn(payload)
    assert la._measured_chars_per_token == pytest.approx((250 + tools_chars) / 100)


def test_effective_chars_per_token_prefers_calibrated_value(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", 2.35)
    assert la._effective_chars_per_token() == 2.35


def test_effective_chars_per_token_falls_back_to_estimate(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    assert la._effective_chars_per_token() == la._CHARS_PER_TOKEN_ESTIMATE


def test_main_resets_calibration_globals_at_start(tmp_path, monkeypatch):
    """A stale calibration from a prior dispatch (or, in-process, a prior
    test) must never leak into a fresh run - main() starts with no
    measurement, matching a cold agent process."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_measured_chars_per_token", 1.5)
    monkeypatch.setattr(la, "_last_prompt_eval_count", 99999)
    monkeypatch.setattr(la, "MAX_STEPS", 1)
    observed = {}

    def _fake_chat(messages):
        observed["last_prompt_eval_count"] = la._last_prompt_eval_count
        observed["measured_chars_per_token"] = la._measured_chars_per_token
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    la.main()
    assert observed["last_prompt_eval_count"] is None
    assert observed["measured_chars_per_token"] is None


def test_main_proactively_trims_when_measured_tokens_near_num_ctx(tmp_path, monkeypatch, capsys):
    """Reacting to a 500 with a bad char estimate is too late: once ollama's
    own measured prompt_eval_count for a turn is already close to NUM_CTX,
    trim the transcript BEFORE the next turn instead of waiting for a request
    to fail first."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 100)
    monkeypatch.setattr(la, "PROACTIVE_TRIM_THRESHOLD", 0.85)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            # Stand in for _stream_one_turn's calibration side effect. Set via
            # monkeypatch, not raw assignment, so these module globals are
            # restored at teardown instead of leaking into later tests.
            monkeypatch.setattr(la, "_last_prompt_eval_count", 90)
            monkeypatch.setattr(la, "_measured_chars_per_token", 1.0)
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    assert "trimming proactively" in out, f"expected a proactive-trim log line, output: {out!r}"


# --- Qwen3 hybrid thinking-mode control (PIPELINE_LOCAL_THINK / LOCAL_AGENT_THINK) ---
#
# Qwen3.6-27B (and other Qwen3 dense models) emit a  Mattis... Mattis reasoning
# block by default. In the tool-calling loop that breaks dispatch: the block
# lands in `content` with no native tool_calls and the driver spins "no tool
# call" forever. Ollama's /api/chat accepts a top-level "think": false to
# suppress the block at the source so the model emits a clean native tool call.
# The flag is opt-in via LOCAL_AGENT_THINK ("false"/"true"); omitted entirely
# when unset so non-Qwen3 models (devstral, gpt-oss, qwen3-coder) get an
# unchanged request body.

def test_ollama_payload_omits_think_by_default(monkeypatch):
    """Unset LOCAL_AGENT_THINK must not add a `think` key — non-Qwen3 models
    get an unchanged /api/chat request."""
    monkeypatch.setattr(la, "THINK", "")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert "think" not in p
    # The rest of the payload is intact.
    assert p["model"] == la.MODEL
    assert p["stream"] is True
    assert p["options"]["num_ctx"] == la.NUM_CTX
    assert p["options"]["temperature"] == la.TEMPERATURE


def test_ollama_payload_think_false_suppresses_qwen3_reasoning(monkeypatch):
    """LOCAL_AGENT_THINK=false adds "think": False so a Qwen3 hybrid model
    skips its  Mattis block and emits a clean native tool call."""
    monkeypatch.setattr(la, "THINK", "false")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] is False


def test_ollama_payload_think_true_when_explicitly_enabled(monkeypatch):
    """LOCAL_AGENT_THINK=true is honored for runs that DO want reasoning."""
    monkeypatch.setattr(la, "THINK", "true")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] is True


def test_ollama_payload_think_unknown_value_is_omitted(monkeypatch):
    """A garbage value must not produce a bogus "think": false that silently
    disables reasoning on a model the caller intended to think. Only the
    exact tokens "true"/"false" opt in; anything else is a no-op."""
    monkeypatch.setattr(la, "THINK", "yes")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert "think" not in p


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_ollama_payload_think_level_is_passed_through(monkeypatch, level):
    """LOCAL_AGENT_THINK accepts the graded-reasoning level strings too (not
    just true/false) - passed through verbatim as Ollama's "think" field.
    Live-validated against gemma4:12b-mlx, which 400s on any value outside
    this set."""
    monkeypatch.setattr(la, "THINK", level)
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] == level


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


def test_exclude_runtime_artifacts_adds_agent_transcript_json(tmp_path, monkeypatch):
    """The transcript-persistence file must be excluded the same way
    agent.log already is, so backend.py's LOCAL_AGENT_TRANSCRIPT_PATH write
    inside the worktree doesn't get swept into commits."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert ".agent_transcript.json" in lines


def test_exclude_runtime_artifacts_agent_transcript_json_not_duplicated(tmp_path, monkeypatch):
    """Calling exclude_runtime_artifacts() twice must not duplicate the
    .agent_transcript.json line, matching the existing idempotent behavior
    for agent.log."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()
    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert lines.count(".agent_transcript.json") == 1


def test_exclude_runtime_artifacts_still_excludes_agent_log_and_pycache(tmp_path, monkeypatch):
    """Regression guard: adding .agent_transcript.json must not remove or
    reorder the pre-existing exclusions."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert "agent.log" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_exclude_runtime_artifacts_hides_transcript_file_from_git_status(tmp_path, monkeypatch):
    """The actual observable behavior this fix delivers: once
    exclude_runtime_artifacts() has run, a real .agent_transcript.json file
    sitting in the worktree must not show up as untracked in `git status`."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()
    (tmp_path / ".agent_transcript.json").write_text('{"messages": []}')

    status = subprocess.run(
        ["git", "status", "--porcelain"], check=False, cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".agent_transcript.json" not in status


# ---------------------------------------------------------------------------
# L1 production side (REVIEWER_ESCALATION_PLAN.md Layer 1): on a CI-fail-rework
# round, reject `done` when the full worktree suite isn't green, feeding the
# failing excerpt back - parallel to the dirty-worktree rejection. Non-rework
# dispatches keep today's behavior (commit-enforced, no suite gate).
# ---------------------------------------------------------------------------

def test_done_rejected_on_rework_round_when_full_suite_fails(tmp_path, monkeypatch, capsys):
    """CI-fail-rework round, clean worktree, but the agent's own test still
    fails: `done` must be rejected, the failing excerpt fed back into the
    conversation, and the loop must NOT exit 0. Bounded by the step cap."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 3)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    excerpt = "FAILED test_rate_limiter.py::test_time_backwards_no_refill - assert 9.0 == 3.0"
    monkeypatch.setattr(la, "_full_suite_result", lambda: (False, excerpt, "test"))

    fake, calls = _sequence_chat([("done", {"summary": "first attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # Not accepted: the suite gate blocked done every step until the cap bound.
    assert rc != 0, f"done must not be accepted while the suite fails; rc={rc}\n{out!r}"
    assert "done rejected — full test suite still fails" in out, out
    # The excerpt was fed back: the second chat() call received it as a user
    # turn (calls[1] is the messages list as seen on the 2nd turn).
    assert len(calls) >= 2
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]
    assert excerpt in last_user["content"], last_user["content"]


def test_done_rejected_message_does_not_presume_the_test_is_wrong(tmp_path, monkeypatch, capsys):
    """Root cause diagnosed live (2026-07-22/23, MODE-29-REVIEW-STORY-LOCK-GUARD):
    this message originated for the CI-fail-rework case, where the failure IS
    always the agent's own test (an oracle-scoped review never saw it). Once
    Gap 1 armed this same gate for ordinary REVIEW rework too, the message's
    flat assertion - "your own committed test has a wrong assertion" -
    became false in that case: the failure can equally be a still-incomplete
    IMPLEMENTATION. Observed consequence: immediately after this exact
    rejection, the agent pivoted to obsessively rewriting its test file for
    ~15 steps instead of fixing the implementation, because the message told
    it the test was the problem. The fed-back content must not assert which
    side is wrong; it must direct the agent to check both and make one
    targeted fix."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 3)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    excerpt = "FAILED test_review_story_lock_guard.py::test_review_story_skips_when_lock_held"
    monkeypatch.setattr(la, "_full_suite_result", lambda: (False, excerpt, "test"))

    fake, calls = _sequence_chat([("done", {"summary": "first attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]["content"]

    assert "your own committed test has a wrong assertion" not in last_user
    assert "implementation" in last_user.lower()
    assert excerpt in last_user


def test_done_accepted_on_non_rework_round_without_consulting_suite(tmp_path, monkeypatch, capsys):
    """Non-rework round, clean worktree: `done` is accepted as today (rc=0)
    and the full suite is NEVER consulted - proves the rework gate is scoped,
    not global."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "would-fail-but-uncalled", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "done"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0  # accepted, as today
    assert suite_calls == []  # suite never consulted on a non-rework round


def test_done_accepted_on_rework_round_when_full_suite_green(tmp_path, monkeypatch):
    """CI-fail-rework round, clean worktree, agent fixed its own test: full
    suite green -> `done` accepted (rc=0). This is the convergence case."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    monkeypatch.setattr(la, "_full_suite_result", lambda: (True, "", None))

    fake, _ = _sequence_chat([("done", {"summary": "fixed"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0


def test_dirty_tree_auto_accept_does_not_bypass_suite_gate(tmp_path, monkeypatch, capsys):
    """Regression guard for a bypass the code review surfaced: the dirty-tree
    auto-accept-at-2 escape must NOT fire on a rework round when the full suite
    still fails. Without the gate at the auto-accept site, an agent could dodge
    the raised done-bar by calling done dirty (reject), done clean+failing-suite
    (reject), done dirty again (done_rejections>=2 -> auto-accept, return 0,
    suite never checked). On a rework round the suite must be checked before
    that escape too; a failing suite rejects instead of auto-accepting, bounded
    by the step cap."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 4)
    # Always dirty: forces every done through the dirty-tree branch, so the
    # auto-accept-at-2 escape is the path under test (the clean-tree suite
    # gate is never reached).
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: None)
    monkeypatch.setattr(la, "_full_suite_result",
                        lambda: (False, "assert 9.0 == 3.0 - test_rate_limiter.py:62", "test"))

    fake, _ = _sequence_chat([("done", {"summary": "bypass attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # The bypass must NOT auto-accept a failing suite.
    assert rc != 0, f"auto-accept-at-2 bypassed the suite gate; rc={rc}\n{out!r}"
    assert "DONE with auto-WIP-commit" not in out, out
    # The suite was checked at the would-be-auto-accept site and rejected.
    assert "done rejected — full test suite still fails" in out, out


def test_rework_suite_reject_cap_parks_instead_of_burning_the_budget(
    tmp_path, monkeypatch, capsys):
    """Regression guard (root-caused live 2026-07-24 on the MODE40 stories):
    on a CI-fail-rework round a model that has corrupted the code and cannot
    green the suite alternates `done` (rejected: suite red) with narration
    ("I cannot resolve this"). That oscillation is invisible to NO_TOOL_CAP
    (a `done` tool call resets consecutive_no_tool) and to the per-target
    repetition guard (done/str_replace are both excluded from it), so before
    the cap the run burned the ENTIRE step/wall-clock budget doing nothing,
    parked, re-dispatched, and repeated — ~20h across 7+ re-dispatches on one
    story. The run must PARK (rc=2) after REWORK_SUITE_REJECT_CAP suite
    rejections, well before MAX_STEPS."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "REWORK_SUITE_REJECT_CAP", 3)
    monkeypatch.setattr(la, "MAX_STEPS", 30)  # far above the cap
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y - assert 1 == 2", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    # _sequence_chat emits `done` for every turn once the script runs out, so
    # this models an agent that keeps calling done on a persistently-red suite.
    fake, calls = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2) at the cap; got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (3) reached" in out, out
    # Parked exactly at the cap — did NOT burn all 30 steps.
    assert len(suite_calls) == 3, f"suite consulted {len(suite_calls)}x, expected 3"
    assert len(calls) <= 4, f"took {len(calls)} turns; expected to park by ~3"


def test_rework_suite_reject_cap_is_driven_by_the_constant(tmp_path, monkeypatch, capsys):
    """The park point honors REWORK_SUITE_REJECT_CAP, not a hardcoded 3: with
    the cap set to 2 the run parks after exactly two suite rejections."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "REWORK_SUITE_REJECT_CAP", 2)
    monkeypatch.setattr(la, "MAX_STEPS", 30)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2); got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (2) reached" in out, out
    assert len(suite_calls) == 2, f"suite consulted {len(suite_calls)}x, expected 2"


# ---------------------------------------------------------------------------
# Mode 40 follow-up: lint feedback, both per-edit (fast, in-run) and as part
# of the done-bar's _full_suite_result (so a rework agent can't exit DONE on
# a lint failure the way the live incident did - see MODE40-CI-ERROR-DETAIL/
# MODE40-LOCAL-LINT-GATE in project memory).
# ---------------------------------------------------------------------------

def test_full_suite_result_runs_lint_after_tests_pass_and_fails_on_lint_error(
    tmp_path, monkeypatch,
):
    """Tests green, lint red -> _full_suite_result reports failure with the
    lint output, not a bare pass. This is the exact gap that let the live
    MODE40-LOCAL-LINT-GATE agent exit DONE on ruff E402/F841 violations."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

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

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, tail, gate = la._full_suite_result()
    assert ok is False
    assert gate == "lint"
    assert "F401" in tail
    assert calls == [["pytest", "-q"], ["ruff", "check", "."]]


def test_full_suite_result_tests_and_lint_both_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: _R())

    ok, tail, gate = la._full_suite_result()
    assert ok is True
    assert tail == ""
    assert gate is None


def test_full_suite_result_skips_lint_when_not_detected(tmp_path, monkeypatch):
    """No lint signal for this repo (detect_lint_command -> None) -> lint is
    never invoked and behavior is unchanged from before this feature."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: None)
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, _tail, gate = la._full_suite_result()
    assert ok is True
    assert gate is None
    assert calls == [["pytest", "-q"]]  # lint subprocess never invoked


def test_full_suite_result_does_not_run_lint_when_tests_fail(tmp_path, monkeypatch):
    """Regression bar: a failing test suite short-circuits before lint runs
    at all - unchanged existing behavior, lint is an additional gate only
    reached once tests are already green."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err="FAILED test_x.py::test_y")

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, tail, gate = la._full_suite_result()
    assert ok is False
    assert gate == "test"
    assert "test_y" in tail
    assert calls == [["pytest", "-q"]]  # lint never invoked


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
    monkeypatch.setattr(la, "CHURN_SAME_PATH_MAX_EDITS", 3)
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
    monkeypatch.setattr(la, "CHURN_SAME_PATH_MAX_EDITS", 3)
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
