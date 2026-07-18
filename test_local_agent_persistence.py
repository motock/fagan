import importlib.util
import json
import os
from pathlib import Path

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
    out, err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid JSON

def test_resume_fallback_invalid_json(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{invalid json", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (empty list)

def test_resume_fallback_invalid_shape(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[]", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (dict instead of list)

def test_resume_fallback_invalid_shape_dict(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"role": "system", "content": "sys"}), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, err = capsys.readouterr()
    assert "RESUME FAILED" in out

# Test resume fallback invalid shape (unknown role value)

def test_resume_fallback_invalid_shape_unknown_role(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps([{"role": "narrator", "content": "sys"}]), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    la = load_module_with_env(env)
    loaded = la._load_resume_transcript()
    assert loaded is None
    out, err = capsys.readouterr()
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
