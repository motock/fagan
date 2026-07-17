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
    "local_agent", str(Path(__file__).parent / "scripts" / "local_agent.py")
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


def test_safe_run_tool_passes_through_normal_results(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.safe_run_tool("create_file", {"path": "new.txt", "content": "hi"})
    assert result == "created new.txt"
    assert (tmp_path / "new.txt").read_text() == "hi"


def test_safe_run_tool_handles_unknown_tool():
    assert la.safe_run_tool("bogus", {}) == "unknown tool bogus"


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
        for line in self._lines:
            yield line


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


def _init_git_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, text=True)


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
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".agent_transcript.json" not in status
