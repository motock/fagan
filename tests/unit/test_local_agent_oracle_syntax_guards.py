"""Tests for the oracle-variant local dispatch agent loop (scripts/local_agent_oracle.py): syntax-rejection escalation, restore_file, and net-progress guards.

Split out of test_local_agent_oracle.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `lao` module itself) moved to tests.unit._local_agent_oracle_test_helpers.
"""
import ast
import subprocess

import pytest

from scripts import local_agent_oracle_repair as laor
from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _FakeProc,
    _init_git_repo,
    _isolate_environ,
    _sequence_chat,
    lao,
    load_oracle_module_with_env,
)


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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "+def bar():\n"
        "    return 2\n"
    )
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
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
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(lao, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(laor, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    result1 = lao.run_tool("create_file", {"path": "ok.py", "content": "x = 1\n"})
    assert result1 == "created ok.py"
    result2 = lao.run_tool("str_replace", {"path": "ok.py", "old_str": "x = 1", "new_str": "x = 2"})
    assert result2 == "edited ok.py"




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
        return (False, "FAILED test_x.py::test_y - assert 1 == 2", "test")

    monkeypatch.setattr(lao, "_full_suite_result", _suite_spy)

    fake, calls = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2) at the cap; got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (3) reached" in out, out
    assert len(suite_calls) == 3, f"suite consulted {len(suite_calls)}x, expected 3"
    assert len(calls) <= 4, f"took {len(calls)} turns; expected to park by ~3"


