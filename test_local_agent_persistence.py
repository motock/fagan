import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    """load_module_with_env below mutates os.environ directly (no
    monkeypatch) so a freshly-imported module reads the intended values at
    import time. Restore the snapshot after every test so those mutations
    never leak into a later test in the same pytest process (see the
    sibling fixture in test_local_agent_oracle.py)."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


# Helper to load module fresh with given env vars

def load_module_with_env(env_vars):
    # scripts/local_agent.py reads LOCAL_AGENT_MODEL at import time
    # (os.environ["LOCAL_AGENT_MODEL"]); set a default before import so the
    # module loads cleanly in any environment, matching the pattern used by
    # the sibling test files (test_local_agent.py, test_local_agent_oracle.py).
    os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
    for k, v in env_vars.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location(
        "local_agent", str(Path(__file__).parent / "scripts" / "local_agent.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# Test persistence writes valid JSON after append

def test_persistence_writes_valid_json(tmp_path, capsys):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    la = load_module_with_env(env)
    messages = la.PersistingList(transcript_path=str(transcript_file))
    msg = {"role": "assistant", "content": "hi"}
    messages.append(msg)
    # Verify file exists and content matches
    assert transcript_file.exists()
    with open(transcript_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == [msg]
    # No temp files left
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert not tmp_files

# Test resume loads valid transcript

def test_resume_loads_valid_transcript(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded == data

# Test resume append content

def test_resume_appends_new_user_turn(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {
        "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file),
        "LOCAL_AGENT_RESUME_APPEND_CONTENT": "feedback"
    }
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    messages = la.PersistingList()
    messages.extend(loaded)
    if loaded and env.get("LOCAL_AGENT_RESUME_APPEND_CONTENT"):
        messages.append({"role": "user", "content": env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]})
    assert messages[-1] == {"role": "user", "content": "feedback"}

# Test resume fallback missing file

def test_resume_fallback_missing_file(tmp_path, capsys):
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(tmp_path / "nonexistent.json")}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, _err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid JSON

def test_resume_fallback_invalid_json(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{invalid json", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, _err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (empty list)

def test_resume_fallback_invalid_shape(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[]", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, _err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (dict instead of list)

def test_resume_fallback_invalid_shape_dict(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"role": "system", "content": "sys"}), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, _err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (unknown role value)

def test_resume_fallback_invalid_shape_unknown_role(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps([{"role": "narrator", "content": "sys"}]), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, _err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test transcript path unset: no file written

def test_no_persistence_when_path_unset(tmp_path):
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": None}
    la = load_module_with_env(env)
    messages = la.PersistingList()
    msg = {"role": "assistant", "content": "hi"}
    messages.append(msg)
    # No file should be created
    files = list(tmp_path.glob("*.json"))
    assert not files

# Test atomic write leaves no temp file behind

def test_atomic_write_no_temp_file(tmp_path):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    la = load_module_with_env(env)
    messages = la.PersistingList(transcript_path=str(transcript_file))
    messages.append({"role": "assistant", "content": "hi"})
    # Ensure no temp files
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert not tmp_files

# Test persistence to a dotfile-named transcript. Production dispatch sets
# LOCAL_AGENT_TRANSCRIPT_PATH = <worktree>/.agent_transcript.json (backend.py
# OllamaDriver.dispatch), and local_agent.py's gitignore lists
# .agent_transcript.json -- so the real transcript filename is a dotfile.
# Existing tests only covered non-dotfile names (transcript.json / resume.json),
# leaving the production path uncovered. This guards that the atomic write
# (tmp file -> os.replace) works when the final filename itself starts with a
# dot (the tmp path is built as f".{name}.tmp", which yields a double-dot
# "..agent_transcript.json.tmp" -- a valid, if ugly, filename, and os.replace
# still renames it onto the dotfile target correctly).

def test_persistence_dotfile_transcript_name(tmp_path, capsys):
    transcript_file = tmp_path / ".agent_transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    la = load_module_with_env(env)
    messages = la.PersistingList(transcript_path=str(transcript_file))
    msg = {"role": "assistant", "content": "hi"}
    messages.append(msg)
    # The transcript file must actually be written.
    assert transcript_file.exists()
    with open(transcript_file, "r", encoding="utf-8") as f:
        assert json.load(f) == [msg]
    # No persistence error should be printed.
    out, _ = capsys.readouterr()
    assert "persistence error" not in out
    # No stray tmp file left behind (the double-dot tmp is replaced away).
    assert not list(tmp_path.glob("*.tmp"))


# ---------- resumed-transcript context-window trimming ----------
#
# Live incident 2026-07-20 (story 93fdc371): a rework resume reuses the
# ENTIRE prior transcript and appends more content (reviewer feedback, a
# tech-lead fix checklist) with no bound. Across repeated rework cycles the
# transcript grew to ~32386 tokens against PIPELINE_LOCAL_NUM_CTX=32768,
# llama.cpp truncated the request, and every retry got an identical 500 (the
# truncated request never changes). _trim_resumed_transcript bounds a
# resumed transcript before it's used, dropping the oldest middle content
# while preserving the original system+task head and the most recent turns.

def _tool_call_message(name, path):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": {"path": path}}}]}


def test_trim_resumed_transcript_noop_when_under_budget(tmp_path):
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "hi"},
    ]
    result = la._trim_resumed_transcript(messages, max_chars=10_000)
    assert result == messages
    assert result is messages


def test_trim_resumed_transcript_preserves_system_and_task_head(tmp_path):
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys" * 50},
        {"role": "user", "content": "task" * 50},
    ]
    for i in range(20):
        messages.append(_tool_call_message("view_file", f"file{i}.py"))
        messages.append({"role": "tool", "content": "x" * 500})
    result = la._trim_resumed_transcript(messages, max_chars=2000)
    assert result[0] == messages[0]
    assert result[1] == messages[1]


def test_trim_resumed_transcript_keeps_most_recent_blocks(tmp_path):
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(20):
        messages.append(_tool_call_message("view_file", f"file{i}.py"))
        messages.append({"role": "tool", "content": "x" * 500})
    result = la._trim_resumed_transcript(messages, max_chars=3000)
    # The last block (file19.py) must survive; the first (file0.py) must not.
    result_json = json.dumps(result)
    assert "file19.py" in result_json
    assert "file0.py" not in result_json


def test_trim_resumed_transcript_never_splits_a_tool_call_from_its_result(tmp_path):
    """An assistant message with tool_calls and the tool-role message(s)
    immediately following it are one block - either both survive or both
    are dropped, never one without the other (a lone tool-role message with
    no preceding tool_calls is an invalid transcript shape some backends
    reject)."""
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(20):
        messages.append(_tool_call_message("view_file", f"file{i}.py"))
        messages.append({"role": "tool", "content": "x" * 500})
    result = la._trim_resumed_transcript(messages, max_chars=3000)
    for i, m in enumerate(result):
        if m.get("role") == "tool":
            assert result[i - 1].get("tool_calls"), (
                f"tool message at index {i} has no preceding assistant "
                "tool_calls message"
            )


def test_trim_resumed_transcript_inserts_note_when_dropping(tmp_path):
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(20):
        messages.append(_tool_call_message("view_file", f"file{i}.py"))
        messages.append({"role": "tool", "content": "x" * 500})
    result = la._trim_resumed_transcript(messages, max_chars=3000)
    note = result[2]
    assert note["role"] == "user"
    assert "dropped" in note["content"]
    assert "context window" in note["content"]


def test_trim_resumed_transcript_prints_diagnostic_on_drop(tmp_path, capsys):
    la = load_module_with_env({})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(20):
        messages.append(_tool_call_message("view_file", f"file{i}.py"))
        messages.append({"role": "tool", "content": "x" * 500})
    la._trim_resumed_transcript(messages, max_chars=3000)
    out, _ = capsys.readouterr()
    assert "RESUME TRIMMED" in out


def test_main_trims_oversized_resumed_transcript_before_first_chat(monkeypatch, tmp_path):
    """End-to-end: main() must apply the trim to a resumed transcript before
    handing it to chat(), using a budget derived from NUM_CTX - not just
    have the helper function exist unused."""
    transcript_file = tmp_path / "resume.json"
    resumed = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(20):
        resumed.append(_tool_call_message("view_file", f"file{i}.py"))
        resumed.append({"role": "tool", "content": "x" * 500})
    transcript_file.write_text(json.dumps(resumed), encoding="utf-8")
    env = {
        "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file),
        "LOCAL_AGENT_NUM_CTX": "256",  # tiny budget forces a real trim
        "LOCAL_AGENT_MAX_STEPS": "1",
    }
    la = load_module_with_env(env)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 1)

    captured = {}

    def _fake_chat(messages):
        captured["messages"] = list(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    monkeypatch.setattr(la, "exclude_runtime_artifacts", lambda: None)

    la.main()

    sent = captured["messages"]
    sent_json = json.dumps(sent)
    assert "file19.py" in sent_json
    assert "file0.py" not in sent_json
    assert sent[0] == resumed[0]
    assert sent[1] == resumed[1]
