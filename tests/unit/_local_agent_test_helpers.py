"""Shared fixtures/helpers for the local dispatch agent loop test suite
(scripts/local_agent.py), split across test_local_agent_*.py files
(originally one 4,441-line test_local_agent.py) to keep each file under the
project's line-count target.

Imports scripts/local_agent.py as a module here (not in each split file) so
every split file shares the SAME loaded `la` module object instead of each
re-executing the ~1,700-line script independently.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

import httpx

from scripts import local_agent_guards as lag
from scripts import local_agent_repair as lar

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


_GENERIC_NUDGE = "Call a tool now (do not write prose)."


class _FakeProc:
    """A stand-in CompletedProcess so the bash handler can format output."""

    def __init__(self):
        self.stdout = "ok"
        self.stderr = ""


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


_OFF_TASK_BRIEF = "Refactor `pipeline/server.py` to add a health-check endpoint."


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
        yield from self._lines


class _FakeStreamCM:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *a):
        return False


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


__all__ = [
    "_GENERIC_NUDGE",
    "_OFF_TASK_BRIEF",
    "_FakeProc",
    "_FakeResp",
    "_FakeStreamCM",
    "_FakeStreamResponse",
    "_init_git_repo",
    "_sequence_chat",
    "_status_error",
    "la",
    "lag",
    "lar",
]
