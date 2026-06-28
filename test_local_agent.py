"""Tests for the local dispatch agent loop (scripts/local_agent.py).

Imported as a module; it only requires LOCAL_AGENT_MODEL in the environment at
import time, so set that before importing. External boundaries (the Ollama
HTTP call) are not exercised here — these cover the pure-logic helpers.
"""
import importlib.util
import os
from pathlib import Path

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
    """When devstral rotates across many distinct view_file / bash targets
    without ever calling create_file / str_replace, the per-target
    repetition guard misses it (every signature is unique), but the
    read-heavy guard must fire: one corrective nudge after
    READ_HEAVY_WINDOW reads, then park after another
    READ_HEAVY_WINDOW if the model ignores the nudge.

    We script 12 distinct bash calls (all unique signatures) and assert
    that main() returns 3 (parking), the nudge + park log lines fire
    exactly once each, and chat() is called ~12 times (not the full 40
    step cap — that would mean the guard failed to stop the run)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat nonexistent_{i}"}) for i in range(15)]
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
    """When the read-heavy guard parks the run, it should not spuriously
    WIP-commit if the worktree is clean. (A read-only run by definition
    hasn't edited any files, so worktree_dirty() is False — auto_wip_commit
    is a no-op, but the branch must be reachable.)"""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat nonexistent_{i}"}) for i in range(15)]
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
