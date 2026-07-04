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
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent_oracle", str(Path(__file__).parent / "scripts" / "local_agent_oracle.py")
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
    assert lao.MUTATING_TOOLS == frozenset({"create_file", "str_replace"})


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
        return None

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)
    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail == "(no acceptance files configured)"
    assert called["run"] is False, "subprocess.run must not be called when ACCEPTANCE_PATHS is empty"


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
    repo = str(Path(__file__).resolve().parent)
    # Prefer the repo's venv python so the test reflects the real launch path;
    # fall back to the active interpreter (sys.executable) in environments
    # without a checked-in .venv — notably CI runners, which install deps into
    # the active interpreter rather than a project venv.
    venv_python = str(Path(repo) / ".venv" / "bin" / "python3")
    if not Path(venv_python).exists():
        venv_python = sys.executable
    r = subprocess.run(
        [venv_python, "-c",
         "import importlib.util, pathlib; "
         f"spec = importlib.util.spec_from_file_location('lao', {str(Path(repo) / 'scripts' / 'local_agent_oracle.py')!r}); "
         "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
         "assert hasattr(m, 'p'), 'pipeline_mcp_server alias not bound'; "
         "assert hasattr(m.p, '_heavy_lock'), 'heavy_lock helper missing'; "
         "assert hasattr(m.p, '_is_heavy'), 'is_heavy helper missing'"],
        cwd=tmp_path,
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
        for line in self._lines:
            yield line


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
