"""Tests for the oracle-variant local dispatch agent loop
(scripts/local_agent_oracle.py).

Imported as a module; it only requires LOCAL_AGENT_MODEL in the environment
at import time. These tests cover the read-heavy guard (Fix A — ported from
local_agent.py) and the str_replace-aware per-target guard (Fix B).
External boundaries (Ollama HTTP, pytest) are not exercised — these cover
the pure-logic helpers.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    """load_oracle_module_with_env below mutates os.environ directly (no
    monkeypatch) so a freshly-imported module reads the intended values at
    import time. Restore the snapshot after every test so those mutations
    never leak into a later test in the same pytest process."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent_oracle", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent_oracle.py")
)
lao = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lao)


def _sequence_chat(responses):
    """Same shape as test_local_agent._sequence_chat: returns a fake chat()
    that yields tool-call responses from `responses` in order, plus a
    recording list."""
    calls = []

    def _fake(messages):
        calls.append(messages)
        idx = len(calls) - 1
        if idx >= len(responses):
            fn, args = "done", {"summary": "out of scripted responses"}
        else:
            fn, args = responses[idx]
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": fn, "arguments": args}}]}

    return _fake, calls


def test_oracle_constants_present():
    """Fix A: the read-heavy guard constants must be defined in the oracle
    harness (they were missing before PR #31, which is why every acceptance-
    backed story slipped past the guard)."""
    assert hasattr(lao, "READ_HEAVY_WINDOW")
    assert lao.READ_HEAVY_WINDOW == 6
    assert hasattr(lao, "MUTATING_TOOLS")
    assert lao.MUTATING_TOOLS == frozenset({"create_file", "str_replace", "replace_lines"})


def test_oracle_create_file_rejects_invalid_python_syntax_diff_artifact(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_rejects_invalid_python_syntax_diff_artifact
    — this guard must be ported to the oracle harness too, or it silently
    regresses for every acceptance-bearing story."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    bad_content = "+def foo():\n+    return 1\n"
    result = lao.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_oracle_create_file_rejects_invalid_python_syntax_dangling_triple_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    bad_content = '"""unterminated docstring\ndef foo():\n    pass\n'
    result = lao.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_oracle_create_file_rejects_return_outside_function(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_rejects_return_outside_function
    - ast.parse() alone accepts this (grammar-only check, doesn't validate
    that `return` sits inside a function); the guard must use compile()."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    bad_content = (
        "def foo():\n"
        "    x = 1\n"
        "for i in range(3):\n"
        "    y = i\n"
        "    return y\n"
    )
    result = lao.run_tool("create_file", {"path": "mod.py", "content": bad_content})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_oracle_try_repair_indentation_fixes_decorator_dedent():
    """Mirrors test_local_agent.test_try_repair_indentation_fixes_decorator_dedent
    - the 14B's @property/column-0 decoding defect (lru_cache t7/t8/t10/t11)
    is cured by a deterministic re-indent of the dedented line to match the
    preceding decorator. Verbatim-mirror guard: both local_agent.py and
    local_agent_oracle.py must carry the fix in lockstep."""
    broken = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self._data = {}\n"
        "    @property\n"
        "def size(self):\n"
        "        return len(self._data)\n"
    )
    repair = lao._try_repair_indentation(broken)
    assert repair is not None
    repaired, note = repair
    assert "    @property\n    def size(self):\n" in repaired
    assert "auto-reindented" in note
    compile(repaired, "<test>", "exec")


def test_oracle_try_repair_indentation_fixes_multiple_dedented_decorators():
    """The decoding defect drops the `def` line after EVERY decorator in the
    file, not just the first (observed live, 2026-07-17, lru_cache: both the
    `@property` getter `def size` AND the `@size.setter` `def size` were
    dedented to column 0). The single-line repair fixed the getter, but the
    setter still broke compile, so the repair returned None and correct code
    was rejected every retry until the wall-clock park. The repair must
    ITERATE. Mirrors test_local_agent.test_try_repair_indentation_fixes_multiple_dedented_decorators."""
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
    repair = lao._try_repair_indentation(broken)
    assert repair is not None, "multi-decorator dedent must be repaired, not rejected"
    repaired, note = repair
    assert "    @property\n    def size(self):\n" in repaired
    assert "    @size.setter\n    def size(self, value):\n" in repaired
    assert "auto-reindented" in note
    compile(repaired, "<test>", "exec")


def test_oracle_create_file_auto_repairs_decorator_dedent_and_writes(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_auto_repairs_decorator_dedent_and_writes."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    broken = (
        "class C:\n"
        "    @property\n"
        "def size(self):\n"
        "        return 1\n"
    )
    result = lao.run_tool("create_file", {"path": "mod.py", "content": broken})
    assert result.startswith("created mod.py")
    assert "auto-reindented" in result
    on_disk = (tmp_path / "mod.py").read_text()
    assert "    @property\n    def size(self):\n" in on_disk
    compile(on_disk, "<test>", "exec")


def test_oracle_create_file_does_not_auto_repair_non_indentation_error(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_does_not_auto_repair_non_indentation_error
    - auto-repair is scoped to IndentationError only."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    bad = "def foo():\n    x = 1\nfor i in range(3):\n    return i\n"
    result = lao.run_tool("create_file", {"path": "mod.py", "content": bad})
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert not (tmp_path / "mod.py").exists() or not (tmp_path / "mod.py").read_text().strip()


def test_oracle_try_repair_indentation_returns_none_when_no_preceding_line():
    assert lao._try_repair_indentation("    x = 1\n") is None


def test_oracle_try_repair_indentation_returns_none_for_valid_content():
    assert lao._try_repair_indentation("def foo():\n    return 1\n") is None


def test_oracle_str_replace_auto_repairs_indentation(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_str_replace_auto_repairs_indentation."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    (tmp_path / "mod.py").write_text("class C:\n    def m(self):\n        return 1\n")
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    def m(self):\n        return 1\n",
        "new_str": "    def m(self):\n        return 1\n    @property\ndef size(self):\n        return 2\n",
    })
    assert result.startswith("edited mod.py")
    assert "auto-reindented" in result
    on_disk = (tmp_path / "mod.py").read_text()
    assert "    @property\n    def size(self):\n" in on_disk
    compile(on_disk, "<test>", "exec")


def test_oracle_no_tool_nudge_escalates_after_consecutive_turns():
    """Mirrors test_local_agent.test_no_tool_nudge_escalates_after_consecutive_turns:
    the narration nudge escalates from a plain call-to-action to behavioral
    guidance (a stuck self-test may be wrong - fix the test, not the impl)."""
    assert lao._no_tool_nudge(1) == "Call a tool now (do not write prose)."
    assert lao._no_tool_nudge(2) == "Call a tool now (do not write prose)."
    escalated = lao._no_tool_nudge(3)
    assert "failing test that you wrote" in escalated
    assert "fix or delete the failing test" in escalated
    assert lao._no_tool_nudge(5) == escalated


def test_oracle_narration_cap_parks_after_consecutive_no_tool_turns(
    tmp_path, monkeypatch, capsys
):
    """Mirrors test_local_agent.test_local_agent_narration_cap_parks_after_consecutive_no_tool_turns:
    the oracle variant must also park after NO_TOOL_CAP consecutive no-tool
    turns instead of burning the full MAX_STEPS budget on a narration loop."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "NO_TOOL_CAP", 5)
    monkeypatch.setattr(lao, "MAX_STEPS", 30)

    def _prose_chat(messages):
        return {"role": "assistant",
                "content": "Next I will run the full test suite to confirm.",
                "tool_calls": []}

    monkeypatch.setattr(lao, "chat", _prose_chat)

    rc = lao.main()

    out = capsys.readouterr().out
    assert rc == 2, f"expected parking exit 2, got {rc}\noutput: {out!r}"
    assert "narration cap (5 consecutive no-tool turns) reached; parking" in out, (
        f"expected narration-cap park line, output: {out!r}"
    )
    assert out.count("no tool call (") == 5, (
        f"expected exactly 5 no-tool turns before the cap, output: {out!r}"
    )

def test_oracle_str_replace_rejects_edit_that_produces_invalid_python_syntax(tmp_path, monkeypatch):
    """A rejected edit must not partially apply — the file's on-disk content
    must be byte-for-byte unchanged from before the call."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = "def foo():\n    return 1\n"
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "+    return 1",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "invalid" in result.lower() and "syntax" in result.lower()
    assert (tmp_path / "mod.py").read_text() == original


def test_oracle_str_replace_rejects_edit_that_orphans_a_referenced_variable(tmp_path, monkeypatch):
    """Ported verbatim from test_local_agent.py; keep both copies in sync.
    Reproduces the exact live failure mode from two separate local models
    (gpt-oss:20b deleting `plan_role_config = _plan_role_config(plan_name)`,
    qwen3-coder:30b deleting `branch = ...`/`worktree = ...`) while a
    reference to the deleted name survived elsewhere in the same function."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def review_story(x):\n"
        "    worktree = x.get('worktree', '')\n"
        "    if x.get('flag'):\n"
        "        return worktree\n"
        "    return None\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    worktree = x.get('worktree', '')\n    if x.get('flag'):",
        "new_str": "    if x.get('flag'):",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "worktree" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_oracle_replace_lines_rejects_edit_that_orphans_a_referenced_variable(tmp_path, monkeypatch):
    """Ported verbatim from test_local_agent.py; keep both copies in sync."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    original = (
        "def f(x):\n"
        "    branch = f'agent/{x}'\n"
        "    worktree = x\n"
        "    return _run(worktree, branch)\n"
    )
    (tmp_path / "mod.py").write_text(original)
    result = lao.run_tool("replace_lines", {
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


def test_oracle_replace_lines_rejects_edit_that_deletes_a_called_module_level_def(
    tmp_path, monkeypatch,
):
    """Ported verbatim from test_local_agent.py; keep both copies in sync."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
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
    result = lao.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 8,
        "end": 8,
        "new_str": "",
    })
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "_review_story_impl" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_oracle_orphaned_variable_check_accepts_edit_that_removes_assignment_and_all_uses(
    tmp_path, monkeypatch,
):
    """Ported verbatim from test_local_agent.py; keep both copies in sync."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text(
        "def f(x):\n"
        "    unused = x\n"
        "    return unused\n"
    )
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    unused = x\n    return unused",
        "new_str": "    return x",
    })
    assert result == "edited mod.py"
    assert (tmp_path / "mod.py").read_text() == "def f(x):\n    return x\n"


def test_oracle_create_file_accepts_valid_python_syntax(tmp_path, monkeypatch):
    """Regression: valid Python content must still write exactly as before."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    result = lao.run_tool("create_file", {"path": "mod.py", "content": "def foo():\n    return 1\n"})
    assert result == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "def foo():\n    return 1\n"


def test_oracle_str_replace_accepts_edit_that_keeps_valid_python_syntax(tmp_path, monkeypatch):
    """Regression: a valid edit must still apply exactly as before."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def foo():\n    return 1\n")
    result = lao.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "return 1",
        "new_str": "return 2",
    })
    assert result == "edited mod.py"
    assert (tmp_path / "mod.py").read_text() == "def foo():\n    return 2\n"


def test_oracle_create_file_syntax_check_only_applies_to_py_paths(tmp_path, monkeypatch):
    """A non-.py path with content that looks like broken Python must not be
    rejected — the ast.parse check is scoped to .py targets only."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    bad_python_looking_content = '"""unterminated\ndef foo(:\n    +return 1\n'
    result = lao.run_tool("create_file", {"path": "README.md", "content": bad_python_looking_content})
    assert result == "created README.md"
    assert (tmp_path / "README.md").read_text() == bad_python_looking_content


def test_oracle_create_file_empty_content_on_py_path_is_not_rejected(tmp_path, monkeypatch):
    """An empty file is valid Python (ast.parse('') does not raise) — must
    not be a false-positive rejection."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    result = lao.run_tool("create_file", {"path": "empty.py"})
    assert result == "created empty.py"
    assert (tmp_path / "empty.py").read_text() == ""


def test_oracle_create_file_overwrites_a_file_it_created_earlier_this_run(tmp_path, monkeypatch):
    """Mirrors test_local_agent's version: a model that mistakenly calls
    create_file again on a path it already successfully created THIS run
    should be allowed to overwrite it - a full rewrite is often the natural
    recovery strategy for a weak model that can't construct a correct
    str_replace old_str. The non-destructive guard exists to protect
    PRE-EXISTING repo/seed files, not files the agent itself just wrote."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_CREATED_THIS_RUN", set())
    r1 = lao.run_tool("create_file", {"path": "mod.py", "content": "x = 1\n"})
    assert r1 == "created mod.py"
    r2 = lao.run_tool("create_file", {"path": "mod.py", "content": "x = 2\n"})
    assert r2 == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "x = 2\n"


def test_oracle_create_file_rejects_overwrite_of_unseen_pre_existing_file(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_rejects_overwrite_of_unseen_pre_existing_file.
    A pre-existing file not created and not viewed this run stays protected
    from blind clobber, and the refusal steers to view_file, not str_replace."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "mod.py").write_text("x = 1\n")
    result = lao.run_tool("create_file", {"path": "mod.py", "content": "x = 2\n"})
    assert result == (
        "ERROR: mod.py already exists and is non-empty. Use view_file to read "
        "it first, then create_file to overwrite it with the full corrected "
        "contents."
    )
    assert (tmp_path / "mod.py").read_text() == "x = 1\n"


def test_oracle_create_file_overwrites_a_pre_existing_file_after_view_file(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_overwrites_a_pre_existing_file_after_view_file.
    Once read via view_file this run, a pre-existing file may be overwritten
    with a full rewrite (unblocks whole-file recovery on resume)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "mod.py").write_text("def merge():\n    raise NotImplementedError\n")
    lao.run_tool("view_file", {"path": "mod.py"})
    result = lao.run_tool("create_file", {"path": "mod.py", "content": "def merge():\n    return []\n"})
    assert result == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == "def merge():\n    return []\n"


def test_oracle_create_file_rejects_overwrite_that_silently_drops_top_level_defs(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_create_file_rejects_overwrite_that_silently_drops_top_level_defs.
    A create_file rewrite of a pre-existing multi-function file that would
    drop top-level def/class not present in the new content must be
    rejected unless confirm_removals=true."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    original = (
        "def ignored_env_vars_present():\n    return []\n\n\n"
        "def read_plist_env():\n    return {}\n\n\n"
        "def read_mcp_server_env():\n    return {}\n\n\n"
        "def _scheduler_plist_path():\n    return None\n\n\n"
        "def _claude_json_path():\n    return None\n"
    )
    (tmp_path / "mod.py").write_text(original)
    lao.run_tool("view_file", {"path": "mod.py"})
    truncated = "def ignored_env_vars_present():\n    return []\n"
    result = lao.run_tool("create_file", {"path": "mod.py", "content": truncated})
    assert result.startswith("ERROR: this create_file overwrite of mod.py would silently drop")
    for name in ("read_plist_env", "read_mcp_server_env", "_scheduler_plist_path", "_claude_json_path"):
        assert name in result
    assert (tmp_path / "mod.py").read_text() == original

    confirmed = lao.run_tool(
        "create_file", {"path": "mod.py", "content": truncated, "confirm_removals": True}
    )
    assert confirmed == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == truncated


def test_oracle_search_tool_returns_actionable_steering_message(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_search_tool_returns_actionable_steering_message.
    There is no 'search' tool; steer toward bash + grep/rg instead of the
    bare generic 'unknown tool search'."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    result = lao.run_tool("search", {"query": "def foo"})
    assert result != "unknown tool search"
    assert "bash" in result
    assert "grep" in result or "rg" in result


def test_oracle_unknown_tool_other_than_search_unchanged(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_unknown_tool_other_than_search_unchanged."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    assert lao.run_tool("nonexistent_tool_xyz", {}) == "unknown tool nonexistent_tool_xyz"


def test_oracle_view_file_returns_requested_line_range(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_returns_requested_line_range."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    lines = [f"line {i} content padding padding padding\n" for i in range(1, 301)]
    (tmp_path / "big.py").write_text("".join(lines))
    result = lao.run_tool("view_file", {"path": "big.py", "line_start": 200, "line_end": 205})
    assert " 200| line 200 content padding padding padding\n" in result
    assert " 205| line 205 content padding padding padding\n" in result
    assert "line 1 content" not in result
    assert "line 300 content" not in result


def test_oracle_view_file_without_range_on_small_file_is_unchanged(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_without_range_on_small_file_is_unchanged."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    result = lao.run_tool("view_file", {"path": "small.py"})
    assert result == "   1| x = 1\n   2| y = 2\n"


def test_oracle_view_file_without_range_on_large_file_includes_continuation_hint(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_without_range_on_large_file_includes_continuation_hint."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    lines = [f"line {i} content padding padding padding\n" for i in range(1, 301)]
    (tmp_path / "big.py").write_text("".join(lines))
    result = lao.run_tool("view_file", {"path": "big.py"})
    assert result.startswith("   1| line 1 content")
    assert "line_start" in result
    assert "line_end" in result
    assert "300" in result


def test_oracle_view_file_line_start_beyond_file_length(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_line_start_beyond_file_length."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    result = lao.run_tool("view_file", {"path": "small.py", "line_start": 50, "line_end": 60})
    assert result.startswith("ERROR")


def test_oracle_view_file_line_end_less_than_line_start(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_line_end_less_than_line_start."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("x = 1\ny = 2\ny = 3\n")
    result = lao.run_tool("view_file", {"path": "small.py", "line_start": 3, "line_end": 1})
    assert result.startswith("ERROR")


def test_oracle_view_file_line_start_zero_is_rejected(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_line_start_zero_is_rejected."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("a\nb\nc\nd\ne\n")
    result = lao.run_tool("view_file", {"path": "small.py", "line_start": 0, "line_end": 2})
    assert result.startswith("ERROR")


def test_oracle_view_file_line_start_negative_is_rejected(tmp_path, monkeypatch):
    """Mirrors test_local_agent.test_view_file_line_start_negative_is_rejected."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_VIEWED_THIS_RUN", set())
    (tmp_path / "small.py").write_text("a\nb\nc\nd\ne\n")
    result = lao.run_tool("view_file", {"path": "small.py", "line_start": -1, "line_end": 2})
    assert result.startswith("ERROR")


def test_oracle_view_file_missing_path_still_required():
    """Mirrors test_local_agent.test_view_file_missing_path_still_required.
    local_agent_oracle's safe_run_tool doesn't have local_agent's required-
    args hint enrichment (no _TOOL_SCHEMAS lookup) — path staying required
    still surfaces as a KeyError via the plain ERROR-prefixed wrapper."""
    result = lao.safe_run_tool("view_file", {"line_start": 1, "line_end": 2})
    assert result.startswith("ERROR running view_file")
    assert "path" in result


def test_oracle_syntax_error_message_includes_lineno_and_offending_line(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: mirrors test_local_agent's version — the rejection must
    name the exact line and quote the offending line plus up to 2 lines of
    context either side, verbatim from the content the model submitted."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
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
    result = lao.run_tool("create_file", {"path": "ctx.py", "content": bad_content})
    assert result.startswith("ERROR")
    assert str(expected_lineno) in result
    assert "+def bar():" in result
    assert "return 1" in result
    assert "return 2" in result


def test_oracle_second_consecutive_syntax_rejection_same_path_carries_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: mirrors test_local_agent's version — resubmitting the
    same broken content for the same path must escalate from the second
    consecutive rejection onward."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    first = lao.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit" not in first.lower()
    second = lao.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" in second.lower()


def test_oracle_second_consecutive_str_replace_rejection_on_large_file_suggests_anchored_edit(
    tmp_path, monkeypatch
):
    """SYNTAX-NUDGE (large file): mirrors test_local_agent's version — a
    str_replace rejection on an existing file above the size threshold
    must NOT get the 'regenerate the entire file' nudge and should get a
    smaller-anchored-edit nudge instead."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    lines = [f"x{i} = {i}\n" for i in range(600)]
    lines.append("def marker():\n    return 1\n")
    (tmp_path / "big.py").write_text("".join(lines))
    old_str = "def marker():\n    return 1"
    new_str = "+def marker():\n    return 1"
    first = lao.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert first.startswith("ERROR")
    second = lao.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" not in second.lower()
    assert "smaller" in second.lower()


def test_oracle_rejection_for_different_path_does_not_inherit_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a rejection for a DIFFERENT path in
    between must not carry the escalation — the counter is per-path."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    lao.run_tool("create_file", {"path": "a.py", "content": bad_content})
    result_b = lao.run_tool("create_file", {"path": "b.py", "content": bad_content})
    assert "do not resubmit" not in result_b.lower()
    result_a_again = lao.run_tool("create_file", {"path": "a.py", "content": bad_content})
    assert "do not resubmit" in result_a_again.lower()


def test_oracle_successful_write_resets_syntax_rejection_counter(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a successful write to a path resets its
    consecutive-rejection counter, so a later rejection for that same path
    starts fresh (no escalation) instead of carrying over stale state."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    lao.run_tool("create_file", {"path": "reset.py", "content": bad_content})
    good = lao.run_tool("create_file", {"path": "reset.py", "content": "def foo():\n    return 1\n"})
    assert good == "created reset.py"
    again = lao.run_tool("str_replace", {
        "path": "reset.py",
        "old_str": "    return 1",
        "new_str": "+    return 1",
    })
    assert again.startswith("ERROR")
    assert "do not resubmit" not in again.lower()


def test_oracle_syntax_rejection_never_writes_file_even_with_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE regression guard: the rejection must still return the
    exact original ERROR-prefixed contract and must NEVER write the file —
    not the submitted content, not a repaired version — even once escalated.
    Guards against silently reintroducing auto-repair."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    bad_content = "+def foo():\n+    return 1\n"
    lao.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    result = lao.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    assert result.startswith("ERROR")
    assert "do not resubmit" in result.lower()
    assert not (tmp_path / "guard.py").exists() or not (tmp_path / "guard.py").read_text().strip()


def test_oracle_valid_python_writes_never_trigger_escalation_text(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: valid .py content must remain entirely unaffected by the
    new rejection-message/escalation machinery."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", {})
    result1 = lao.run_tool("create_file", {"path": "ok.py", "content": "x = 1\n"})
    assert result1 == "created ok.py"
    result2 = lao.run_tool("str_replace", {"path": "ok.py", "old_str": "x = 1", "new_str": "x = 2"})
    assert result2 == "edited ok.py"


class _FakeProc:
    def __init__(self):
        self.stdout = "ok"
        self.stderr = ""


@pytest.mark.parametrize("cmd", [
    "git reset --hard",
    "git reset --hard b5063c7",
    "git clean -fd",
    "git clean -xf",      # force, f not first — must still be caught
    "git checkout -- .",
    "git restore src/app.py",
    "git status && git reset --hard b5063c7",
])
def test_oracle_bash_blocks_destructive_git_ops(tmp_path, monkeypatch, cmd):
    """Mode 3a: the destructive-git guard must be ported to the oracle harness
    too, or acceptance-bearing stories silently regress. A destructive op must
    be refused before subprocess.run is reached."""
    monkeypatch.setattr(lao, "CWD", tmp_path)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not be called for a destructive git op")

    monkeypatch.setattr(lao.subprocess, "run", _boom)
    result = lao.run_tool("bash", {"command": cmd})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "blocked" in result.lower()


@pytest.mark.parametrize("cmd", [
    "git status",
    "git add -A",
    "git checkout feature-branch",
    "git reset HEAD src/app.py",
    "git checkout --theirs src/app.py",
    "pytest -q",
])
def test_oracle_bash_passes_non_destructive_commands_through(tmp_path, monkeypatch, cmd):
    """Non-destructive commands must still run on the oracle harness."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    seen = {}

    def _fake_run(c, **k):
        seen["cmd"] = c
        return _FakeProc()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)
    result = lao.run_tool("bash", {"command": cmd})
    assert seen["cmd"] == cmd
    assert result == "ok"


# ---------- restore_file tool (ported from local_agent.py, 2026-07-22) ----------

def test_oracle_restore_file_reverts_to_last_commit(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    f = tmp_path / "a.py"
    f.write_text("original\n")
    subprocess.run(["git", "add", "a.py"], check=False, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=False, cwd=tmp_path, capture_output=True)
    f.write_text("a mess\n")

    result = lao.run_tool("restore_file", {"path": "a.py"})

    assert not result.startswith("ERROR"), f"unexpected error: {result}"
    assert f.read_text() == "original\n"


def test_oracle_restore_file_requires_path():
    result = lao.run_tool("restore_file", {})
    assert result.startswith("ERROR")


def test_oracle_destructive_git_op_error_points_to_restore_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    result = lao.run_tool("bash", {"command": "git reset --hard HEAD"})
    assert "restore_file" in result


# ---------- net-progress guard (ported from local_agent.py, 2026-07-22) ----------

def test_oracle_net_progress_guard_parks_after_max_steps_with_no_mutation(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "NET_PROGRESS_MAX_STEPS", 3)
    monkeypatch.setattr(lao, "READ_HEAVY_WINDOW", 1000)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(10)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 3, f"expected net-progress park, got rc={rc}\noutput: {out!r}"
    assert "no successful edit in 3 steps" in out, f"output: {out!r}"
    assert len(calls) == 3, f"expected 3 chat() calls before parking, got {len(calls)}"


def test_oracle_net_progress_guard_resets_on_successful_mutation(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "NET_PROGRESS_MAX_STEPS", 3)
    monkeypatch.setattr(lao, "READ_HEAVY_WINDOW", 1000)
    responses = (
        [("bash", {"command": "cat a"}), ("bash", {"command": "cat b"})]
        + [("create_file", {"path": "new.py", "content": "# real code\n"})]
        + [("bash", {"command": "cat c"})]
        + [("done", {"summary": "wrote the module"})]
    )
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 0, (
        f"the mutation should reset the counter so the run reaches done, "
        f"not park; got rc={rc}\noutput: {out!r}"
    )


def test_oracle_rework_suite_reject_cap_parks_instead_of_burning_the_budget(
    tmp_path, monkeypatch, capsys,
):
    """Parity with test_local_agent's suite-reject-cap guard: on a rework round
    where the acceptance oracle is green but the full suite stays red, the
    oracle driver must PARK (rc=2) after REWORK_SUITE_REJECT_CAP done-rejections
    rather than re-prompting until MAX_STEPS."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "REWORK_SUITE_REJECT_CAP", 3)
    monkeypatch.setattr(lao, "MAX_STEPS", 30)
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, ""))

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y - assert 1 == 2")

    monkeypatch.setattr(lao, "_full_suite_result", _suite_spy)

    fake, calls = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2) at the cap; got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (3) reached" in out, out
    assert len(suite_calls) == 3, f"suite consulted {len(suite_calls)}x, expected 3"
    assert len(calls) <= 4, f"took {len(calls)} turns; expected to park by ~3"


# ---------- view_file range-aware repetition signature (ported, 2026-07-22) ----------

def test_oracle_view_file_different_ranges_do_not_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
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
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" not in out, f"output: {out!r}"


def test_oracle_view_file_same_range_three_times_still_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 100, "line_end": 150})
        for _ in range(4)
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, f"output: {out!r}"


def test_oracle_read_heavy_loop_nudges_once_then_parks(tmp_path, monkeypatch, capsys):
    """Strict/wedging path on the oracle harness: when devstral RE-READS an
    already-seen target without ever calling create_file/str_replace, the
    read-heavy guard must fire — one nudge after READ_HEAVY_WINDOW reads,
    then park after another READ_HEAVY_WINDOW if the model ignores it.

    Mirrors test_local_agent.test_local_agent_read_heavy_loop_nudges_once_then_parks
    but on the oracle harness — which previously had no such guard. Uses
    the re-reading scenario (a target repeated within the post-nudge
    window, each target <= 2 total so the per-target guard stays out of
    the way) so this pins the strict park at 2 * READ_HEAVY_WINDOW.
    Distinct-exploration leniency is covered by
    test_oracle_read_heavy_distinct_exploration_reaches_an_edit."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    # Oracle harness doesn't have a [boot] line, so we just need any tool
    # activity in the log. 6 distinct reads (nudge) + a post-nudge window
    # of 6 where 'cat g' repeats once (re-reading = wedging).
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
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, (
        f"expected exactly 1 read-heavy nudge, output: {out!r}"
    )
    assert "[parking: read-heavy after nudge]" in out, (
        f"expected parking after nudge, output: {out!r}"
    )
    # chat() called at most 2 * READ_HEAVY_WINDOW times (12 default).
    assert len(calls) <= 12, (
        f"guard should stop the run well before MAX_STEPS=30; got {len(calls)}"
    )


def test_oracle_park_disabled_continues_past_strict_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False on the oracle harness: the read-heavy guard still
    nudges and still prints the park line, but does NOT terminate (return 3).
    Mirrors test_local_agent.test_local_agent_park_disabled_continues_past_strict_park
    so the kill-switch is pinned on both harnesses (acceptance-bearing stories
    use the oracle). Same re-reading scenario as
    test_oracle_read_heavy_loop_nudges_once_then_parks, which parks at call 12
    by default; disabled parking must progress past it."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "PARK_ENABLED", False)
    responses = [
        ("bash", {"command": "cat a"}), ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}), ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}), ("bash", {"command": "cat f"}),
        ("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}), ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"nudge must still fire, output: {out!r}"
    assert "[parking: read-heavy after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    assert len(calls) > 12, (
        f"disabled parking should let the run continue past the strict-park "
        f"point, got {len(calls)} chat calls"
    )


def test_oracle_read_heavy_distinct_exploration_reaches_an_edit(tmp_path, monkeypatch, capsys):
    """Lenient path on the oracle harness: distinct-target exploration
    (reading many files once each) that reaches an edit must NOT park at
    2 * READ_HEAVY_WINDOW. Mirrors the base-harness test of the same name.

    Pre-fix (flat 12-read cutoff) this parked at step 12 before the
    create_file, returning 3. Post-fix it reaches the edit and the empty
    acceptance oracle accepts done -> exit 0."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(16)]
    responses.append(("create_file", {"path": "new_module.rs", "content": "// real code\n"}))
    responses.append(("done", {"summary": "wrote the module"}))
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"distinct exploration that reaches an edit must finish, got rc={rc}\noutput: {out!r}"
    )
    assert "[parking: read-heavy" not in out, (
        f"all-distinct exploration must not park before the bounded cap; output: {out!r}"
    )


def test_oracle_read_heavy_distinct_exploration_is_bounded(tmp_path, monkeypatch, capsys):
    """The leniency for distinct exploration is BOUNDED on the oracle
    harness too: a run that keeps reading distinct targets with NO
    eventual mutation still parks, at ~24 reads (not the flat 12). Mirrors
    the base-harness test of the same name."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(30)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 3, f"unbounded distinct reading must still park, got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert "distinct windows" in out, (
        f"distinct-cap park must log a distinct-windows message; output: {out!r}"
    )
    assert len(calls) > 12, (
        f"distinct exploration must get more than the flat 12 reads; got {len(calls)}"
    )
    assert len(calls) <= 24, (
        f"distinct exploration must be bounded (<= 24 reads); got {len(calls)}"
    )


def test_oracle_read_heavy_distinct_constants():
    """The exploration-aware guard's leniency constant must be defined on
    the oracle harness too (the Mode 3a lesson: a loop-guard change in the
    base harness silently regressed the oracle variant when only the base
    was patched)."""
    assert lao.READ_HEAVY_WINDOW == 6
    assert hasattr(lao, "READ_HEAVY_DISTINCT_WINDOWS")
    assert lao.READ_HEAVY_DISTINCT_WINDOWS == 3


def test_oracle_does_not_nudge_with_regular_writes(tmp_path, monkeypatch, capsys):
    """Fix A: a model that interleaves one mutating call per window must
    NOT be flagged. The deque mixes reads and writes, so the all-non-
    mutating check never holds.

    Mirrors the equivalent test in test_local_agent.py for the oracle
    harness."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "echo 1 > /dev/null"}),
        ("view_file", {"path": "fake.py"}),
        ("bash", {"command": "echo 2 > /dev/null"}),
        ("view_file", {"path": "fake2.py"}),
        ("bash", {"command": "echo 3 > /dev/null"}),
        # Write in the middle of the deque so the all-non-mutating
        # check never holds.
        ("create_file", {"path": "new_module.rs", "content": "// real code\n"}),
        ("bash", {"command": "echo 4 > /dev/null"}),
        ("view_file", {"path": "fake3.py"}),
        ("bash", {"command": "echo 5 > /dev/null"}),
        ("view_file", {"path": "fake4.py"}),
        ("bash", {"command": "echo 6 > /dev/null"}),
        # done gets rejected (worktree dirty), auto-WIP commits on retry.
        ("done", {"summary": "wrote the module"}),
        ("done", {"summary": "wrote the module"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[read-heavy nudge:" not in out, (
        f"nudge must NOT fire when at least one mutating call sits in the "
        f"sliding window; output: {out!r}"
    )
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_str_replace_repetitions_do_not_fire_per_target_guard(
    tmp_path, monkeypatch, capsys,
):
    """Fix B on the oracle harness: str_replace calls to the same path
    don't trip the per-target guard, even though the path is repeated.
    Mirrors test_local_agent.test_local_agent_str_replace_repetitions_do_not_fire_per_target_guard
    but for the oracle variant.

    Note: the oracle harness auto-commits via auto_commit (named
    differently from the base harness's auto_wip_commit)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        ("bash", {"command": "echo a > /dev/null"}),
        ("bash", {"command": "echo b > /dev/null"}),
        ("bash", {"command": "echo c > /dev/null"}),
        # Six str_replace calls to the SAME path. Pre-fix code would
        # park at the 4th. Post-fix code ignores str_replace in the
        # per-target counter. The oracle harness auto-commits on done
        # once the worktree is dirty, so a single done() call here
        # succeeds (after the first done, worktree_dirty() is True,
        # but the oracle's done handler runs the oracle grading and
        # returns 0 once pytest passes — we stub pytest out via the
        # empty ACCEPTANCE_PATHS, which short-circuits oracle_result
        # to True at line 165-167).
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub", "new_str": "// stub 1",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 1", "new_str": "// stub 2",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 2", "new_str": "// stub 3",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 3", "new_str": "// stub 4",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 4", "new_str": "// stub 5",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 5", "new_str": "// stub 6",
        }),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"per-target guard should ignore str_replace repetitions; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"per-target guard should not park on str_replace; output: {out!r}"
    )
    # The oracle harness accepts done() when worktree_dirty() is True
    # AND the oracle_result() returns True (which it does in this test
    # because ACCEPTANCE_PATHS is empty -> line 165 short-circuits).
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_repeated_create_file_on_existing_path_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent.test_local_agent_repeated_create_file_on_
    existing_path_trips_repetition_guard for the oracle harness: create_file's
    own membership in MUTATING_TOOLS made `if fn in MUTATING_TOOLS:
    seen.clear()` wipe its own signature's count on every call, so repeated
    create_file attempts against an existing path could never accumulate
    past 1 and the per-target guard was permanently inert for this pattern."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [("create_file", {"path": "lru_cache.py", "content": "class LRUCache:\n    x = 1\n"})
                 for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
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


def test_oracle_interleaved_failed_mutations_still_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent's version: reproduces the real 2026-07-15
    lru_cache incident where failed str_replace/bash calls interleaved
    between failed create_file attempts kept wiping create_file's
    accumulating failure count (clearing was gated on tool identity, not
    outcome), making the guard inert for this exact pattern."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [
        ("create_file", {"path": "lru_cache.py", "content": "content1"}),
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE", "new_str": "x"}),
        ("bash", {"command": "true"}),
        ("create_file", {"path": "lru_cache.py", "content": "content2"}),
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE2", "new_str": "x"}),
        ("bash", {"command": "true"}),
        ("create_file", {"path": "lru_cache.py", "content": "content3"}),
        ("create_file", {"path": "lru_cache.py", "content": "content4"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
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


def test_oracle_edit_between_reads_resets_per_target_repetition_counter(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent.test_local_agent_edit_between_reads_resets_
    per_target_repetition_counter for the oracle harness: the per-target
    guard's counter must reset on a real edit (str_replace/create_file), not
    stay a lifetime cumulative count across the whole run. Without this,
    view_file(X), view_file(X), str_replace(X), view_file(X), view_file(X)
    hits the >=3 threshold on the second post-edit view from stale pre-edit
    reads, even though a real edit happened in between."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
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
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"a real edit between reads must reset the per-target counter; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"a real edit between reads must not lead to a park; output: {out!r}"
    )
    # Same accept path as the str_replace test above: done() on a dirty
    # worktree succeeds because ACCEPTANCE_PATHS is empty here.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_detects_pytest_when_pyproject_present(tmp_path, monkeypatch):
    """When the project has pyproject.toml, detect_test_command returns
    ['pytest']; the oracle must append ACCEPTANCE_PATHS + the quiet flags."""
    # ACCEPTANCE_PATHS is read at module import time, so we set it directly
    # on the module instead of via the env var.
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_x.py"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 't'\n")

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # Pretend pytest ran and the test passed.
        class _R:
            returncode = 0
            stdout = "1 passed"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail.endswith("1 passed"), f"expected tail to include pytest stdout; got {tail!r}"
    argv = captured["argv"]
    # detect_test_command returns ['pytest'] for pyproject; oracle appends
    # acceptance paths + quiet flags.
    assert argv[0] == "pytest", f"expected pytest as argv[0], got {argv!r}"
    assert "tests/test_x.py" in argv, f"acceptance path should be appended; got {argv!r}"
    assert "-q" in argv and "--no-header" in argv and "no:cacheprovider" in argv


def test_oracle_detects_cargo_when_cargo_toml_present(tmp_path, monkeypatch):
    """When the project has Cargo.toml, detect_test_command returns
    ['cargo', 'test']; the oracle must run it bare (no pytest flags) so the
    project's [[test]] wiring picks up the acceptance file."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/acceptance.rs"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 't'\n")

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        class _R:
            returncode = 0
            stdout = "test result: ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, tail = lao.oracle_result()
    assert ok is True
    assert "ok" in tail
    argv = captured["argv"]
    # cargo test runs as-is — no pytest paths, no -q flag, no
    # no:cacheprovider. The cargo test runner discovers the .rs file via
    # the project's own [[test]] entries in Cargo.toml.
    assert argv[:2] == ["cargo", "test"], f"expected ['cargo', 'test'], got {argv!r}"
    assert "-q" not in argv, f"cargo does not want pytest flags; got {argv!r}"
    assert "tests/acceptance.rs" not in argv, (
        f"for cargo we run the bare command, not the acceptance path; got {argv!r}"
    )


def test_oracle_uses_npm_test_when_package_json_present(tmp_path, monkeypatch):
    """When the project has package.json, detect_test_command returns
    ['npm', 'test']; the oracle runs it bare."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test/acceptance.test.js"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "package.json").write_text('{"name": "t"}\n')

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        class _R:
            returncode = 0
            stdout = "1 passing"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, _ = lao.oracle_result()
    assert ok is True
    argv = captured["argv"]
    assert argv[:2] == ["npm", "test"], f"expected ['npm', 'test'], got {argv!r}"
    assert "-q" not in argv


def test_oracle_returns_true_with_short_circuit_when_no_acceptance_paths(tmp_path, monkeypatch):
    """Regression guard: empty ACCEPTANCE_PATHS still short-circuits
    regardless of the project's test framework. Prevents the
    detect_test_command path from running when the oracle was launched
    without an acceptance list (the existing 'fall back to model `done`'
    warning path in main())."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    # pyproject.toml present — without the short-circuit, detect_test_command
    # would run and pytest would be invoked.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 't'\n")
    called = {"run": False}

    def _fake_run(*a, **kw):
        called["run"] = True

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)
    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail == "(no acceptance files configured)"
    assert called["run"] is False, "subprocess.run must not be called when ACCEPTANCE_PATHS is empty"


# ---------- oracle-file tamper protection ----------
# create_file/str_replace are blocked from touching the acceptance path
# directly (is_oracle_path), but that guard is a no-op for the bash tool,
# which can rm/overwrite/sed-in-place the file with no such check. A model
# could dodge the guard entirely via shell. These lock in both the existing,
# previously-untested create_file/str_replace guard and the new bash-tamper
# detection (snapshot-and-restore, since arbitrary shell syntax can't be
# reliably pattern-matched).

def test_create_file_on_oracle_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")

    result = lao.run_tool("create_file", {"path": "test_acceptance.py", "content": "def test_x(): pass\n"})

    assert "must NOT be modified" in result
    assert (tmp_path / "test_acceptance.py").read_text() == "def test_x(): assert True\n"


def test_str_replace_on_oracle_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")

    result = lao.run_tool("str_replace", {
        "path": "test_acceptance.py", "old_str": "assert True", "new_str": "assert False",
    })

    assert "must NOT be modified" in result
    assert (tmp_path / "test_acceptance.py").read_text() == "def test_x(): assert True\n"


def test_bash_deleting_oracle_file_is_restored(tmp_path, monkeypatch):
    """The concrete gap: is_oracle_path only guards create_file/str_replace,
    so a model could `rm` the acceptance file via bash to dodge it entirely.
    The snapshot-and-restore check must put it back and warn, regardless of
    what shell command was used to remove it."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    original = "def test_x(): assert True\n"
    (tmp_path / "test_acceptance.py").write_text(original)
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "rm test_acceptance.py"})

    assert (tmp_path / "test_acceptance.py").exists()
    assert (tmp_path / "test_acceptance.py").read_text() == original
    assert "restored" in result.lower()


def test_bash_overwriting_oracle_file_is_restored(tmp_path, monkeypatch):
    """Same gap, different shell technique: overwriting via redirection
    instead of deleting. Must be caught the same way."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    original = "def test_x(): assert True\n"
    (tmp_path / "test_acceptance.py").write_text(original)
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "echo 'def test_x(): pass' > test_acceptance.py"})

    assert (tmp_path / "test_acceptance.py").read_text() == original
    assert "restored" in result.lower()


def test_bash_not_touching_oracle_file_is_unaffected(tmp_path, monkeypatch):
    """The restore check must not false-positive on ordinary bash calls that
    never touch the oracle file at all."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "echo hello"})

    assert "restored" not in result.lower()
    assert "hello" in result


def test_oracle_script_importable_from_non_pipeline_cwd(tmp_path):
    """Regression guard for the PR #32 fix: the oracle harness must
    import pipeline_mcp_server at module load (reused for _heavy_lock +
    _is_heavy + _checkpoint_impl), and the import requires sys.path to
    contain the pipeline repo. local_agent.py has the sys.path.insert at
    line 56; the oracle variant was missing it until PR #32. Without the
    insert, every oracle subprocess failed at import with
    ModuleNotFoundError — the agent never reached main(), so check_story_status
    saw an empty agent.log and treated it as a failed launch.

    We assert by running `python -c "import scripts.local_agent_oracle"`
    with cwd=tmp_path (the worktree) and PYTHONPATH pointing at the
    pipeline repo. The script's own sys.path.insert handles the
    pipeline_mcp_server import, so this should succeed silently."""
    import subprocess
    import sys
    repo = str(Path(__file__).resolve().parent.parent.parent)
    # Prefer the repo's venv python so the test reflects the real launch path;
    # fall back to the active interpreter (sys.executable) in environments
    # without a checked-in .venv — notably CI runners, which install deps into
    # the active interpreter rather than a project venv.
    venv_python = str(Path(repo) / ".venv" / "bin" / "python3")
    if not Path(venv_python).exists():
        venv_python = sys.executable
    r = subprocess.run(
        [venv_python, "-c",
         ("import importlib.util, pathlib; "
         f"spec = importlib.util.spec_from_file_location('lao', {str(Path(repo) / 'scripts' / 'local_agent_oracle.py')!r}); "
         "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
         "assert hasattr(m, 'p'), 'pipeline_mcp_server alias not bound'; "
         "assert hasattr(m.p, '_heavy_lock'), 'heavy_lock helper missing'; "
         "assert hasattr(m.p, '_is_heavy'), 'is_heavy helper missing'")],
        check=False, cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, (
        f"oracle import from non-pipeline cwd failed:\n"
        f"  stdout: {r.stdout}\n  stderr: {r.stderr}"
    )


# ---------- chat() streaming + retry (2026-06-28 timeout incident) ----------
# The original chat() was a single blocking httpx.post(stream=False, timeout=900s)
# with NO retry. One transient Ollama queue stall killed the whole run after 7-11
# steps of real progress (all 3 e2e agents died at "LLM call failed: timed out"
# mid-iteration). The new chat() streams (per-chunk silence timeout) and retries
# transient failures. These tests pin that contract by mocking _stream_one_turn
# (the per-attempt seam) and httpx.stream (the streaming seam).

class _FakeResp:
    """Minimal stand-in for an httpx.Response — only status_code is read."""
    def __init__(self, code):
        self.status_code = code


def _status_error(code):
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=httpx.Request("POST", "http://localhost"),
        response=_FakeResp(code),
    )


def test_chat_retries_on_timeout_then_succeeds(monkeypatch):
    """A transient read timeout must not kill the run: chat() retries and
    returns the message once the stall clears."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)  # no real backoff in tests
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("read timed out")
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lao, "_stream_one_turn", _flaky)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3, f"expected 3 attempts (2 timeouts + success), got {calls['n']}"
    assert msg["content"] == "done"


def test_chat_raises_after_max_attempts_on_persistent_timeout(monkeypatch):
    """If every attempt times out, chat() exhausts CHAT_MAX_ATTEMPTS then
    re-raises — main()'s except then commits WIP and returns 1. This matches
    the old terminal behavior, but only after genuinely trying."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    monkeypatch.setattr(lao, "CHAT_MAX_ATTEMPTS", 3)
    calls = {"n": 0}

    def _always_timeout(payload):
        calls["n"] += 1
        raise httpx.TimeoutException("read timed out")

    monkeypatch.setattr(lao, "_stream_one_turn", _always_timeout)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected TimeoutException after exhausting attempts"
    except httpx.TimeoutException:
        pass
    assert calls["n"] == 3, f"expected exactly CHAT_MAX_ATTEMPTS=3 attempts, got {calls['n']}"


def test_chat_does_not_retry_on_4xx(monkeypatch):
    """4xx is a bad request — retrying is pointless and just burns time.
    chat() must raise immediately on the first 4xx."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(payload):
        calls["n"] += 1
        raise _status_error(404)

    monkeypatch.setattr(lao, "_stream_one_turn", _bad_request)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(404)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, f"4xx must NOT be retried; got {calls['n']} attempts"


def test_chat_retries_on_5xx_then_succeeds(monkeypatch):
    """5xx is a transient server error (Ollama model-not-loaded, OOM) —
    chat() retries it like a transport error."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_stream_one_turn", _flaky_5xx)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


# ---------- chat() provider routing (LOCAL_AGENT_PROVIDER, S3) ----------
# Mirrors test_local_agent.py's provider-routing tests: ollama (default)
# keeps the streaming _stream_one_turn path untouched; any other provider
# (lmstudio, mlx) goes through the blocking _provider_chat_turn seam,
# delegating to inference_providers.get_local_provider().chat().

def test_chat_default_provider_is_ollama():
    assert lao.PROVIDER == "ollama"


def test_chat_uses_stream_one_turn_when_provider_is_ollama(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "ollama")

    def _boom(messages):
        raise AssertionError("_provider_chat_turn must not be called for ollama")

    monkeypatch.setattr(lao, "_provider_chat_turn", _boom)
    monkeypatch.setattr(
        lao, "_stream_one_turn",
        lambda payload: {"role": "assistant", "content": "ok"},
    )
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"


def test_chat_routes_through_provider_when_not_ollama(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")

    def _boom(payload):
        raise AssertionError("_stream_one_turn must not be called for lmstudio")

    monkeypatch.setattr(lao, "_stream_one_turn", _boom)
    monkeypatch.setattr(
        lao, "_provider_chat_turn",
        lambda messages: {"role": "assistant", "content": "from lmstudio",
                           "tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]},
    )
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "from lmstudio"
    assert msg["tool_calls"][0]["function"]["name"] == "done"


def test_provider_chat_turn_extracts_message_from_envelope(monkeypatch):
    captured = {}

    class _FakeProvider:
        def chat(self, messages, *, model, num_ctx, temperature, tools, endpoint, timeout):
            captured.update(model=model, num_ctx=num_ctx, temperature=temperature,
                             tools=tools, endpoint=endpoint, timeout=timeout)
            return {"message": {"role": "assistant", "content": "hi"},
                    "prompt_eval_count": 3, "eval_count": 5}

    monkeypatch.setattr(lao.inference_providers, "get_local_provider", lambda: _FakeProvider())
    msg = lao._provider_chat_turn([{"role": "user", "content": "hi"}])
    assert msg == {"role": "assistant", "content": "hi"}
    assert captured["model"] == lao.MODEL
    assert captured["tools"] == lao.TOOLS


def test_chat_retries_on_provider_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_provider_chat_turn", _flaky_5xx)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


def test_chat_does_not_retry_on_provider_4xx(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(lao, "_provider_chat_turn", _bad_request)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(400)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


def test_chat_retries_on_provider_rate_limited_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "mlx")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _rate_limited_then_ok(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lao.inference_providers.RateLimitedError("429")
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_provider_chat_turn", _rate_limited_then_ok)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


class _FakeStreamResponse:
    """Stand-in for the response object httpx.stream() yields. raise_for_status
    honors the status code; iter_lines yields the canned JSON lines."""
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


def test_stream_one_turn_assembles_streamed_chunks(monkeypatch):
    """_stream_one_turn accumulates content across streamed JSON chunks and
    returns the assembled message (same shape the old r.json()['message']
    returned). A done:true chunk terminates the stream."""
    # Simulate Ollama streaming: content split across 3 chunks, then a
    # done:true terminator. devstral emits tool calls as text content, so
    # tool_calls stays None and recover_tool_calls parses content downstream.
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create "}}),
        json.dumps({"message": {"role": "assistant", "content": "a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    msg = lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."
    assert "tool_calls" not in msg  # none emitted in this stream


def test_stream_one_turn_captures_native_tool_calls(monkeypatch):
    """For models that use native tool_calls (not devstral's text style),
    _stream_one_turn captures them from the done chunk and attaches them to
    the assembled message so main()'s m.get('tool_calls') sees them."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": ""}}),
        json.dumps({"message": {"role": "assistant", "content": ""},
                     "tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"command": "ls"}}}],
                     "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    msg = lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["tool_calls"] == [{"function": {"name": "bash",
                                                "arguments": {"command": "ls"}}}]


def test_stream_one_turn_raises_on_5xx(monkeypatch):
    """A 5xx response makes raise_for_status raise HTTPStatusError, which
    chat() then retries. _stream_one_turn itself must surface it."""
    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse([], status_code=500))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    try:
        lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
        assert False, "expected HTTPStatusError(500)"
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 500


def test_stream_one_turn_uses_configurable_connect_timeout(monkeypatch):
    """connect=10.0 was hardcoded and too short for a cold local-model load -
    observed live 2026-07-13: both glm-4.7-flash (~17s cold load) and
    qwen3-coder:30b timed out identically. Ollama aborts the in-flight load
    and frees the memory it had claimed the instant the client gives up, so
    a too-short connect timeout causes an infinite load/abort/retry cycle
    that never completes rather than a genuine failure. CONNECT_TIMEOUT_
    SECONDS makes this configurable like the other chat-retry knobs
    (READ_SILENCE_SECONDS, CHAT_MAX_ATTEMPTS)."""
    monkeypatch.setattr(lao, "CONNECT_TIMEOUT_SECONDS", 45.0)
    captured = {}

    def _fake_stream(method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert captured["timeout"].connect == 45.0


# ---------- live chars/token calibration from ollama's own prompt_eval_count
# (ported from local_agent.py, 2026-07-29) ----------

def test_oracle_stream_one_turn_captures_prompt_eval_count_and_calibrates_ratio(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    payload = {"model": "x", "messages": [{"role": "user", "content": "x" * 250}],
               "tools": [], "stream": True}
    lao._stream_one_turn(payload)
    assert lao._last_prompt_eval_count == 100
    # 250 message chars + len("[]") for the empty tools schema, over 100
    # measured prompt tokens (see the tools-schema test below).
    assert lao._measured_chars_per_token == pytest.approx((250 + 2) / 100)


def test_oracle_stream_one_turn_leaves_calibration_unset_without_prompt_eval_count(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", None)
    lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert lao._last_prompt_eval_count is None
    assert lao._measured_chars_per_token is None


def test_oracle_stream_one_turn_counts_tools_schema_in_calibration(monkeypatch):
    """The tools schema is counted in prompt_eval_count, so it must be in the
    numerator too - see local_agent.py's copy of this test for the measured
    bias (0.67 vs a true ~2.35 on an early turn) this guards against."""
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    tools = [{"type": "function", "function": {"name": "x" * 146}}]
    tools_chars = len(json.dumps(tools))
    payload = {"model": "x", "messages": [{"role": "user", "content": "y" * 250}],
               "tools": tools, "stream": True}
    lao._stream_one_turn(payload)
    assert lao._measured_chars_per_token == pytest.approx((250 + tools_chars) / 100)


def test_oracle_effective_chars_per_token_prefers_calibrated_value(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", 2.35)
    assert lao._effective_chars_per_token() == 2.35


def test_oracle_effective_chars_per_token_falls_back_to_estimate(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    assert lao._effective_chars_per_token() == lao._CHARS_PER_TOKEN_ESTIMATE


def test_oracle_main_resets_calibration_globals_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "_measured_chars_per_token", 1.5)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", 99999)
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    observed = {}

    def _fake_chat(messages):
        observed["last_prompt_eval_count"] = lao._last_prompt_eval_count
        observed["measured_chars_per_token"] = lao._measured_chars_per_token
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    lao.main()
    assert observed["last_prompt_eval_count"] is None
    assert observed["measured_chars_per_token"] is None


def test_oracle_main_proactively_trims_when_measured_tokens_near_num_ctx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 100)
    monkeypatch.setattr(lao, "PROACTIVE_TRIM_THRESHOLD", 0.85)
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
            monkeypatch.setattr(lao, "_last_prompt_eval_count", 90)
            monkeypatch.setattr(lao, "_measured_chars_per_token", 1.0)
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    assert "trimming proactively" in out, f"expected a proactive-trim log line, output: {out!r}"


# ---------- main() trims and retries once on a persistent 5xx (ported from
# local_agent.py — this variant was missing the recovery entirely, so an
# oversized transcript on an acceptance-bearing dispatch (the production
# path: backend.py routes every story with an `acceptance` block here) died
# on the first unrecoverable 5xx instead of shrinking and retrying) ----------

def test_oracle_main_trims_transcript_and_retries_once_on_persistent_5xx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    # A tiny NUM_CTX means genuine post-head content (grown by the two
    # successful reads below) already exceeds the trim budget, so trimming
    # reliably triggers without needing to hand-construct a huge transcript.
    monkeypatch.setattr(lao, "NUM_CTX", 10)
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

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected the trim-and-retry to recover, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 4, (
        f"expected exactly 4 chat() calls (2 reads, fail once, succeed on "
        f"the trim-retry), got {calls['n']}\noutput: {out!r}"
    )
    assert "escalating trim and retrying" in out, f"expected the escalation trim log line, output: {out!r}"


def test_oracle_main_gives_up_when_trim_retry_also_fails(tmp_path, monkeypatch, capsys):
    """The trim-retry is exactly one extra attempt, not another open-ended
    loop: if chat() still fails after the trim, main() must give up (return 1)
    rather than retrying indefinitely."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        raise _status_error(500)

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected give-up after the trim-retry also fails, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 4, (
        f"expected exactly 4 chat() calls (2 reads + original failure + one "
        f"trim-retry), got {calls['n']}\noutput: {out!r}"
    )


# ---------- 5xx escalation must not crash the oracle agent loop (2026-07-30) ----------
# Ported from test_local_agent.py: recover_from_oversized_5xx only catches
# httpx.HTTPStatusError, so a 4xx it re-raises or a non-HTTP backend failure
# (TransportError, RateLimitedError) propagates out of the helper. It is called
# from inside main()'s except-HTTPStatusError handler, and a sibling
# except-Exception does NOT catch exceptions raised from within another except
# body - so without the guard at the call site such a failure escapes main() and
# kills the run, regressing the original "must not crash the agent loop"
# invariant. The oracle runs the production path for acceptance-bearing
# dispatches, so it must carry the same guard as local_agent.py.

def test_oracle_main_does_not_crash_on_transport_error_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A TransportError raised by chat() during an escalation round must give up
    (return 1), not propagate out of main() and crash the oracle agent loop."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise httpx.TransportError("connection reset")  # escalation round failure

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
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


def test_oracle_main_does_not_crash_on_4xx_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A 4xx re-raised by the escalation helper must give up (return 1), not
    escape main() and crash the oracle agent loop - matching the original
    trim-retry path, which caught every failure and returned 1."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise _status_error(400)  # escalation round re-raises a 4xx

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
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


# ---------- transcript persistence + resume (ported from local_agent.py, see
# test_local_agent_persistence.py for the reference test suite) ----------
# These tests need a fresh module import per test with different env vars
# (LOCAL_AGENT_RESUME_TRANSCRIPT_PATH etc. are read at call time via
# os.environ.get, but loading a fresh module keeps each test isolated from
# the module-level `lao` instance shared by the rest of this file).

def load_oracle_module_with_env(env_vars):
    os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
    for k, v in env_vars.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location(
        "local_agent_oracle", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent_oracle.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_oracle_persistence_writes_valid_json(tmp_path):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList(transcript_path=str(transcript_file))
    msg = {"role": "assistant", "content": "hi"}
    messages.append(msg)
    assert transcript_file.exists()
    with open(transcript_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == [msg]
    assert not list(tmp_path.glob("*.tmp"))


def test_oracle_resume_loads_valid_transcript(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    assert mod._load_resume_transcript() == data


def test_oracle_resume_appends_new_user_turn(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {
        "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file),
        "LOCAL_AGENT_RESUME_APPEND_CONTENT": "feedback",
    }
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    messages = mod.PersistingList()
    messages.extend(loaded)
    if loaded and env.get("LOCAL_AGENT_RESUME_APPEND_CONTENT"):
        messages.append({"role": "user", "content": env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]})
    assert messages[-1] == {"role": "user", "content": "feedback"}
    assert len(messages) == 3


def test_oracle_resume_fallback_missing_file(tmp_path, capsys):
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(tmp_path / "nonexistent.json")}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_json(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{invalid json", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_empty_list(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[]", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_dict(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"role": "system", "content": "sys"}), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_unknown_role(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps([{"role": "narrator", "content": "sys"}]), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_no_persistence_when_path_unset(tmp_path):
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": None}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList()
    messages.append({"role": "assistant", "content": "hi"})
    assert not list(tmp_path.glob("*.json"))


def test_resume_env_helper_sets_var_for_isolation_regression(tmp_path):
    """Companion to test_resume_env_does_not_leak_into_next_test below: sets
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH via the helper so the next test (which
    must run immediately after this one - see its docstring) can assert it
    doesn't survive into a fresh test's os.environ."""
    dummy = tmp_path / "resume.json"
    load_oracle_module_with_env({"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(dummy)})
    assert os.environ["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(dummy)


def test_resume_env_does_not_leak_into_next_test():
    """Regression for the test-isolation leak: load_oracle_module_with_env
    mutates os.environ directly with no teardown, so a var set by the
    previous test (see above) silently survives into this one and into any
    dispatch test that happens to run afterward in the same pytest process
    (e.g. test_dispatch_omits_resume_transcript_path_when_unset in
    test_backend.py, which builds its subprocess env from os.environ).
    This test is order-dependent by design: it must run directly after
    test_resume_env_helper_sets_var_for_isolation_regression (this repo's
    pyproject.toml configures no test-randomization plugin, so pytest's
    default in-file execution order is deterministic)."""
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in os.environ


def test_oracle_atomic_write_no_temp_file(tmp_path):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList(transcript_path=str(transcript_file))
    messages.append({"role": "assistant", "content": "hi"})
    assert not list(tmp_path.glob("*.tmp"))


def test_oracle_main_uses_fresh_messages_when_no_resume_path(monkeypatch, tmp_path):
    """When LOCAL_AGENT_RESUME_TRANSCRIPT_PATH is unset, main() must build the
    fresh [system, user-task] pair, matching local_agent.py's fallback."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_TRANSCRIPT_PATH", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_RESUME_APPEND_CONTENT", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "do the task")
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "1")
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])

    captured = {}

    def _fake_chat(messages):
        captured["messages"] = list(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, ""))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "exclude_runtime_artifacts", lambda: None)

    lao.main()

    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "do the task"}


def test_oracle_main_resumes_transcript_and_skips_fresh_pair(monkeypatch, tmp_path):
    """When LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid transcript,
    main() must load it instead of building the fresh system/task pair."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    transcript_file = tmp_path / "resume.json"
    resumed = [{"role": "system", "content": "orig sys"},
               {"role": "user", "content": "orig task"},
               {"role": "assistant", "content": "prior reply"}]
    transcript_file.write_text(json.dumps(resumed), encoding="utf-8")
    monkeypatch.setenv("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH", str(transcript_file))
    monkeypatch.delenv("LOCAL_AGENT_TRANSCRIPT_PATH", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_RESUME_APPEND_CONTENT", "reviewer feedback")
    monkeypatch.setenv("LOCAL_AGENT_TASK", "do the task")
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])

    captured = {}

    def _fake_chat(messages):
        captured["messages"] = list(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, ""))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "exclude_runtime_artifacts", lambda: None)

    lao.main()

    messages = captured["messages"]
    assert messages[0] == resumed[0]
    assert messages[1] == resumed[1]
    assert messages[2] == resumed[2]
    assert messages[3] == {"role": "user", "content": "reviewer feedback"}


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


def test_oracle_exclude_runtime_artifacts_adds_agent_transcript_json(tmp_path, monkeypatch):
    """The transcript-persistence file must be excluded the same way
    agent.log already is, so backend.py's LOCAL_AGENT_TRANSCRIPT_PATH write
    inside the worktree doesn't get swept into commits."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert ".agent_transcript.json" in lines


def test_oracle_exclude_runtime_artifacts_agent_transcript_json_not_duplicated(tmp_path, monkeypatch):
    """Calling exclude_runtime_artifacts() twice must not duplicate the
    .agent_transcript.json line, matching the existing idempotent behavior
    for agent.log."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()
    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert lines.count(".agent_transcript.json") == 1


def test_oracle_exclude_runtime_artifacts_still_excludes_agent_log_and_pycache(tmp_path, monkeypatch):
    """Regression guard: adding .agent_transcript.json must not remove or
    reorder the pre-existing exclusions."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert "agent.log" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_oracle_exclude_runtime_artifacts_hides_transcript_file_from_git_status(tmp_path, monkeypatch):
    """The actual observable behavior this fix delivers: once
    exclude_runtime_artifacts() has run, a real .agent_transcript.json file
    sitting in the worktree must not show up as untracked in `git status`."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()
    (tmp_path / ".agent_transcript.json").write_text('{"messages": []}')

    status = subprocess.run(
        ["git", "status", "--porcelain"], check=False, cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".agent_transcript.json" not in status


def test_recover_tool_calls_repairs_python_triple_quoted_arguments():
    """Kept in sync with test_local_agent's copy: the oracle agent (which the
    benchmark harness dispatches through) must also salvage a str_replace whose
    multi-line code argument is emitted with Python triple-quote syntax, or the
    edit is silently dropped (observed with Qwen2.5-Coder-14B-4bit on mlx)."""
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
    out = lao.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "str_replace"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "rate_limiter.py"
    assert "def __init__(self, capacity):" in args["new_str"]


def test_recover_tool_calls_tolerates_raw_newlines_in_json_string():
    """Kept in sync with test_local_agent's copy: a distinct malformation
    from the triple-quote case (observed live, 2026-07-17, Qwen2.5-Coder-14B-
    4bit on mlx, interval_merge task, the benchmark harness this oracle agent
    is dispatched through) - the model uses ordinary double-quoted JSON
    string syntax for a create_file's multi-line `content` argument, but
    embeds RAW literal newline bytes instead of escaping them as `\\n`. A raw
    json.loads rejects this ("Invalid control character"), the triple-quote
    repair does not apply, and the tool call was silently dropped every
    retry until the wall-clock park with the real fix never landing."""
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
    out = lao.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "create_file"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "intervals.py"
    assert "def merge(x):" in args["content"]
    assert "return x" in args["content"]


def test_recover_tool_calls_returns_none_on_non_toolcall_prose():
    """The repair pass must fail closed on ordinary prose - no phantom call."""
    assert lao.recover_tool_calls("Looks good, nothing left to change.") is None


# ---------------------------------------------------------------------------
# L1: harness-enforced full-suite done-bar on CI-fail rework rounds.
# (REVIEWER_ESCALATION_PLAN.md Layer 1.) On a rework round triggered by a
# merge-gate CI failure, the agent's own broken test is the defect, but the
# acceptance oracle excludes that test file - so oracle-green must NOT be
# allowed to terminate the loop. finish_if_green must additionally require the
# full worktree suite green, feeding the failing excerpt back. Cold-start
# behavior stays byte-for-byte identical (oracle-green remains the bar).
# ---------------------------------------------------------------------------

def _finish_if_green_spy(monkeypatch, *, oracle_ok, full_ok, full_tail=""):
    """Wire oracle_result / _full_suite_result / auto_commit / worktree_dirty
    to deterministic fakes and return (messages, commits, full_calls) so a test
    can assert on termination + fed-back excerpt + commit side effects."""
    messages: list = []
    commits: list = []
    full_calls: list = []

    def _oracle():
        return (oracle_ok, "(oracle stub)")

    def _full():
        full_calls.append(True)
        return (full_ok, full_tail)

    monkeypatch.setattr(lao, "oracle_result", _oracle)
    monkeypatch.setattr(lao, "_full_suite_result", _full)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: commits.append(reason))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: True)
    return messages, commits, full_calls


def test_finish_if_green_cold_start_terminates_on_oracle_green_only(monkeypatch):
    """Cold start (REWORK_FULL_SUITE unset): oracle green is the done-bar and
    the full suite is NEVER consulted - even when it would fail. Proves the
    rework gate is scoped, not global, so a fresh dispatch's behavior is
    unchanged."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="would-fail-but-uncalled"
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", False)
    assert lao.finish_if_green(3, messages=messages) is True
    assert commits == ["feat: implement task (acceptance oracle green)"]
    assert full_calls == []  # the full suite was not run on a cold start


def test_finish_if_green_rework_round_blocks_done_when_full_suite_fails(monkeypatch):
    """CI-fail rework round: oracle green but the agent's own test still fails.
    finish_if_green must NOT terminate, must NOT commit, and must feed the
    failing-test excerpt back into messages so the agent works the broken
    assertion on the next loop iteration instead of declaring done."""
    excerpt = "AssertionError: assert 9.0 == 3.0  - test_rate_limiter.py:62"
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail=excerpt
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(7, messages=messages) is False
    assert commits == []  # no auto-commit while the agent's own test still fails
    assert full_calls == [True]
    # The failing excerpt was fed back as a user turn so the model sees it.
    assert any(m["role"] == "user" and excerpt in m["content"] for m in messages), messages


def test_finish_if_green_rework_round_terminates_when_full_suite_green(monkeypatch):
    """CI-fail rework round: agent fixed its own test, full suite now green
    alongside the oracle. finish_if_green must terminate and commit - the
    raised done-bar is satisfied. This is the convergence case."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=True, full_tail=""
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(9, messages=messages) is True
    assert commits == ["feat: implement task (acceptance oracle green)"]
    assert full_calls == [True]


def test_finish_if_green_rework_oracle_not_green_returns_false(monkeypatch):
    """Oracle not green: no termination regardless of rework flag - the oracle
    is still the primary bar; the full suite is an additional gate on top."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=False, full_ok=True, full_tail=""
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(2, messages=messages) is False
    assert commits == []
    assert full_calls == []  # short-circuited: oracle not green, suite not run


# ---------------------------------------------------------------------------
# Unbounded finish_if_green rework loop (found live 2026-07-29 on
# TRANSPORT-ALIAS-DEPRECATION): the `done` handler bounds its full-suite
# rejections with REWORK_SUITE_REJECT_CAP and parks (rc 2) once exhausted, but
# finish_if_green - the OTHER termination path, which fires automatically after
# every create_file/str_replace/bash - had no such bound. When the full suite
# could not be greened (there, a rework-authored test that contradicted the
# read-only acceptance oracle), it printed "ORACLE GREEN but full suite still
# fails" and returned False on every single mutating step until the step cap,
# burning the entire budget with no progress and no park signal.
# ---------------------------------------------------------------------------

def test_finish_if_green_parks_after_suite_reject_cap(monkeypatch):
    """finish_if_green must bound its full-suite rejections the same way the
    `done` handler does. After REWORK_SUITE_REJECT_CAP consecutive oracle-
    green/suite-red calls it must signal park rather than returning False
    forever, so an unsatisfiable suite ends the run instead of burning every
    remaining step."""
    messages, _commits, _full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="1 failed"
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "REWORK_SUITE_REJECT_CAP", 3)
    lao._reset_suite_rejections()

    # The first CAP-1 rejections keep the agent working the failure.
    assert lao.finish_if_green(1, messages=messages) is False
    assert lao.finish_if_green(2, messages=messages) is False
    assert lao.suite_reject_cap_reached() is False

    # The CAP'th rejection trips the bound.
    assert lao.finish_if_green(3, messages=messages) is False
    assert lao.suite_reject_cap_reached() is True


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
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (True, ""))
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

    ok, tail = lao._full_suite_result()
    assert ok is False
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
    ok, tail = lao._full_suite_result()
    assert ok is True
    assert tail == ""


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
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (False, excerpt))
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
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (False, excerpt))
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
        lambda: suite_calls.append(True) or (False, "would-fail-but-uncalled"),
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
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (True, ""))
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

    ok, tail = lao._full_suite_result()
    assert ok is False
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

    ok, tail = lao._full_suite_result()
    assert ok is True
    assert tail == ""


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

    ok, _tail = lao._full_suite_result()
    assert ok is True
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

    ok, tail = lao._full_suite_result()
    assert ok is False
    assert "test_y" in tail
    assert calls == [["pytest", "-q"]]


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
