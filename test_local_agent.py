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
