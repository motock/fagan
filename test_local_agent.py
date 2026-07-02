"""Tests for the local dispatch agent loop (scripts/local_agent.py).

Imported as a module; it only requires LOCAL_AGENT_MODEL in the environment at
import time, so set that before importing. External boundaries (the Ollama
HTTP call) are not exercised here — these cover the pure-logic helpers.
"""
import importlib.util
import json
import os
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
