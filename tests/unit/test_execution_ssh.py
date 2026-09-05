"""Unit tests for the SSH execution seam in pipeline.execution."""

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Import the module under test
import pipeline.execution as exec_mod

# Helper to read spec file

def read_spec(path: str) -> dict:
    return json.loads(Path(path).read_text())


# Test _remap_path_prefix directly

def test_remap_path_prefix():
    local = "/local/worktree"
    remote = "/remote/worktree"
    assert exec_mod._remap_path_prefix("/local/worktree/foo", local, remote) == "/remote/worktree/foo"
    assert exec_mod._remap_path_prefix("/local/worktree", local, remote) == "/remote/worktree"
    assert exec_mod._remap_path_prefix("/local/other", local, remote) == "/local/other"


# Test missing environment variables raise ValueError

def test_spawn_ssh_missing_env_vars():
    cwd = Path("/tmp/worktree")
    log_path = Path("/tmp/log.txt")
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError) as excinfo:
            exec_mod._spawn_ssh(["cmd"], cwd=cwd, log_path=log_path, append=False)
        assert "PIPELINE_REMOTE_EXEC_HOST" in str(excinfo.value)
        assert "PIPELINE_REMOTE_SYNC_ROOT" in str(excinfo.value)

    with mock.patch.dict(os.environ, {"PIPELINE_REMOTE_EXEC_HOST": "host"}, clear=True):
        with pytest.raises(ValueError) as excinfo:
            exec_mod._spawn_ssh(["cmd"], cwd=cwd, log_path=log_path, append=False)
        assert "PIPELINE_REMOTE_SYNC_ROOT" in str(excinfo.value)

    with mock.patch.dict(os.environ, {"PIPELINE_REMOTE_SYNC_ROOT": "/remote"}, clear=True):
        with pytest.raises(ValueError) as excinfo:
            exec_mod._spawn_ssh(["cmd"], cwd=cwd, log_path=log_path, append=False)
        assert "PIPELINE_REMOTE_EXEC_HOST" in str(excinfo.value)


# Test successful spawn with mocked subprocess and git

def test_spawn_ssh_success(monkeypatch):
    cwd = Path("/tmp/worktree")
    log_path = Path("/tmp/log.txt")
    host = "example.com"
    sync_root = "/remote"
    branch_name = "main"

    # Mock environment
    monkeypatch.setenv("PIPELINE_REMOTE_EXEC_HOST", host)
    monkeypatch.setenv("PIPELINE_REMOTE_SYNC_ROOT", sync_root)

    # Mock git command to return branch name
    def mock_check_output(cmd, text=True, **kw):
        assert cmd == ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"]
        return branch_name + "\n"
    monkeypatch.setattr(exec_mod.subprocess, "check_output", mock_check_output)

    # Mock subprocess.Popen to return a fake process with pid 4242
    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
    fake_proc = FakeProc(4242)
    called_args = {}
    def mock_popen(args, cwd=None, env=None, stdout=None, stderr=None):
        called_args["args"] = args
        called_args["cwd"] = cwd
        called_args["env"] = env
        called_args["stdout"] = stdout
        called_args["stderr"] = stderr
        return fake_proc
    monkeypatch.setattr(exec_mod.subprocess, "Popen", mock_popen)

    # Mock open to capture mode
    open_calls = []
    def mock_open(file, mode='r', *args, **kwargs):
        open_calls.append((file, mode))
        return mock.mock_open(read_data="").return_value
    monkeypatch.setattr("builtins.open", mock_open)

    # Prepare command and env with a path that needs remapping
    cmd = [str(cwd / ".venv/bin/python"), "x"]
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(cwd / ".agent_transcript.json")}

    handle = exec_mod._spawn_ssh(cmd, cwd=cwd, log_path=log_path, append=True, env=env)

    # Verify returned handle
    assert handle.pid == 4242
    assert handle.model == ""

    # Verify log file opened in append mode
    assert any(mode == "a" for _, mode in open_calls)

    # Verify subprocess.Popen called with expected args
    remote_url = f"ssh://{host}{sync_root}/repo-bare.git"
    remote_cwd = f"{sync_root}/worktrees/{cwd.name}"
    spec_path = called_args["args"][-1]
    assert spec_path.endswith(".json")

    # Verify spec file contents
    spec = read_spec(spec_path)
    assert spec["cmd"] == [
        f"{remote_cwd}/.venv/bin/python",
        "x",
    ]
    assert spec["env"] == {
        "LOCAL_AGENT_TRANSCRIPT_PATH": f"{remote_cwd}/.agent_transcript.json"
    }
    assert spec["remote_cwd"] == remote_cwd
    assert spec["branch"] == branch_name

    # Verify Popen arguments
    assert called_args["args"] == [
        sys.executable,
        "-m",
        "pipeline.remote_exec",
        "--worktree",
        str(cwd),
        "--remote-url",
        remote_url,
        "--host",
        host,
        "--spec-file",
        spec_path,
    ]
    # stdout and stderr should be the same file object
    assert called_args["stdout"] is called_args["stderr"]

    # Test append=False mode
    open_calls.clear()
    handle = exec_mod._spawn_ssh(cmd, cwd=cwd, log_path=log_path, append=False, env=env)
    assert any(mode == "w" for _, mode in open_calls)


def test_spawn_ssh_closes_spec_fd_when_json_dump_raises(tmp_path, monkeypatch):
    """The spec-file descriptor must be closed even when json.dump raises.

    _spawn_ssh's writer does spec_file = os.fdopen(fd, "w"); json.dump(...);
    spec_file.close() with no try/finally, so an exception from json.dump
    (or anything else between open and close) skips the close() call and
    leaks the fd for the life of the process.
    """
    monkeypatch.setenv("PIPELINE_REMOTE_EXEC_HOST", "gpu-host")
    monkeypatch.setenv("PIPELINE_REMOTE_SYNC_ROOT", "/srv/sync")
    monkeypatch.setattr(exec_mod.subprocess, "check_output", lambda *a, **kw: "main\n")

    opened_files = []
    real_fdopen = os.fdopen

    def spy_fdopen(fd, mode="r", *args, **kwargs):
        f = real_fdopen(fd, mode, *args, **kwargs)
        opened_files.append(f)
        return f

    monkeypatch.setattr(exec_mod.os, "fdopen", spy_fdopen)

    def raising_dump(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(exec_mod.json, "dump", raising_dump)

    cwd = tmp_path / "worktree"
    cwd.mkdir()
    log_path = tmp_path / "log.txt"

    with pytest.raises(RuntimeError):
        exec_mod._spawn_ssh(
            ["echo", "hi"],
            cwd=cwd,
            log_path=log_path,
            append=False,
            env={"ANTHROPIC_API_KEY": "sk-ant-test"},
        )

    assert opened_files, "expected os.fdopen to be called while building the spec file"
    assert opened_files[0].closed, (
        "spec file descriptor must be closed even when json.dump raises, "
        "else it leaks for the life of the process"
    )

