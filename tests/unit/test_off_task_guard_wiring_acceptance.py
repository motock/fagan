"""Acceptance test for wiring the off-task-drift guard (Mode 31) into
scripts/local_agent.py's main() loop. Drives the real la.main() entrypoint
with chat() mocked at its true external boundary -- the same pattern the
existing read-heavy/repetition guard tests use -- so the oracle grades
whether the wiring actually landed in main(), not just whether the pure
helpers from the prior story exist in isolation.
"""
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


def _sequence_chat(responses):
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


def test_off_task_edit_nudges_once_and_does_not_park_on_a_single_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Fix the bug in `pipeline/server.py`.")
    responses = [
        ("create_file", {"path": "unrelated_thing.py", "content": "x = 1\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, f"expected exactly 1 nudge, output: {out!r}"
    assert rc == 0, f"single stray file must not park, got rc={rc}\noutput: {out!r}"


def test_off_task_edits_on_two_distinct_paths_park_after_the_nudge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Fix the bug in `pipeline/server.py`.")
    responses = [
        ("create_file", {"path": "unrelated_one.py", "content": "x = 1\n"}),
        ("create_file", {"path": "unrelated_two.py", "content": "y = 2\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, f"expected exactly 1 nudge, output: {out!r}"
    assert "[parking: off-task drift" in out, f"expected a park line, output: {out!r}"
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"


def test_on_task_edits_never_nudge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Fix the bug in `pipeline/server.py`.")
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "server.py").write_text("x = 1\n")
    responses = [
        ("str_replace", {"path": "pipeline/server.py", "old_str": "x = 1", "new_str": "x = 2"}),
        ("done", {"summary": "done"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, f"unexpected nudge, output: {out!r}"
    assert rc == 0


def test_brief_naming_no_paths_never_nudges(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Fix the login bug.")
    responses = [
        ("create_file", {"path": "anything.py", "content": "x = 1\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, f"unexpected nudge, output: {out!r}"
    assert rc == 0
