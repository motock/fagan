"""Shared fixtures/helpers for the oracle-variant local dispatch agent loop
test suite (scripts/local_agent_oracle.py), split across
test_local_agent_oracle_*.py files (originally one 3,564-line
test_local_agent_oracle.py) to keep each file under the project's
line-count target.

Imports scripts/local_agent_oracle.py as a module here (not in each split
file) so every split file shares the SAME loaded `lao` module object
instead of each re-executing the script independently.
"""
import importlib.util
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


class _FakeProc:
    def __init__(self):
        self.stdout = "ok"
        self.stderr = ""


class _FakeResp:
    """Minimal stand-in for an httpx.Response — only status_code is read."""
    def __init__(self, code):
        self.status_code = code


def _status_error(code):
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=httpx.Request("POST", "http://localhost"),
        response=_FakeResp(code),
    )


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


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


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
        return (full_ok, full_tail, None if full_ok else "test")

    monkeypatch.setattr(lao, "oracle_result", _oracle)
    monkeypatch.setattr(lao, "_full_suite_result", _full)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: commits.append(reason))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: True)
    return messages, commits, full_calls


__all__ = [
    "_FakeProc",
    "_FakeResp",
    "_FakeStreamCM",
    "_FakeStreamResponse",
    "_finish_if_green_spy",
    "_init_git_repo",
    "_isolate_environ",
    "_sequence_chat",
    "_status_error",
    "lao",
    "load_oracle_module_with_env",
]
