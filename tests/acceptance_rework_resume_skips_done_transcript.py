"""Acceptance: a rework redispatch must not resume a prior transcript that ends
with the agent calling its completion tool ``done``.

``resume_via_transcript`` only ever checked that the transcript file EXISTS,
never the state it ends in. An agent whose previous run ended by claiming
completion is resumed with its own "I already finished" turn as the most recent
state, which dominates the reviewer-feedback turn appended after it: it
re-emits ``done`` without editing anything, burning the rework budget against an
unchanged HEAD.

A transcript ending any other way is still safe to resume, so those paths must
keep resuming (the controls below) - and an unreadable transcript must fail
OPEN (resume) rather than newly breaking dispatch.

Fixtures are resolved by name via ``request.getfixturevalue`` rather than by
same-named test parameters: this file is a digest-pinned read-only oracle, so it
must not need a pyproject per-file-ignore to lint clean.
"""
import json

from app import backend
from pipeline import dispatch as pd
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)

_PLAN = "resumedone"
_FEEDBACK = "The SQL is injectable; parameterize it."
_APPEND = (
    "The code reviewer REQUESTED CHANGES on your previous attempt. "
    "Address this feedback:\n" + _FEEDBACK
)


def _done_turn():
    """The exact trailing entry a run that ends on its completion tool leaves:
    the assistant message carrying the ``done`` tool call."""
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "function": {"index": 0, "name": "done",
                                      "arguments": {"summary": "committed and done"}}}]}


def _dispatch(monkeypatch, request, transcript_text):
    """Drive a real rework dispatch; return the env handed to the agent process."""
    plans = request.getfixturevalue("plan_dir")
    roots = request.getfixturevalue("worktree_root")
    request.getfixturevalue("agents_dir")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    wt = roots / "S1"
    wt.mkdir()
    (wt / ".agent_transcript.json").write_text(transcript_text)
    _write_manifest(plans, _PLAN, {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(wt),
               "review_feedback": _FEEDBACK},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda cmd, env=None, **kw: (calls.append({"cmd": cmd, "env": env}),
                                     _FakeProc(6001))[1])
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    p.dispatch_story(_PLAN, "S1")
    return calls[0]["env"]


def _transcript(*turns):
    return json.dumps([{"role": "system", "content": "sys"},
                       {"role": "user", "content": "task"}, *turns])


def test_done_ending_transcript_is_not_resumed(monkeypatch, request):
    env = _dispatch(monkeypatch, request, _transcript(_done_turn()))
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in env
    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in env


def test_tool_result_ending_transcript_still_resumes(monkeypatch, request):
    """A run that did NOT end by claiming completion is still safe to resume."""
    env = _dispatch(monkeypatch, request,
                    _transcript(_done_turn(), {"role": "tool", "content": "1 passed"}))
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == _APPEND
    assert str(env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"]).endswith(
        ".agent_transcript.json")


def test_plain_assistant_ending_transcript_still_resumes(monkeypatch, request):
    """An assistant turn with no tool calls at all is not a completion claim."""
    env = _dispatch(monkeypatch, request,
                    _transcript({"role": "assistant", "content": "Working on it."}))
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == _APPEND


def test_unreadable_transcript_fails_open(tmp_path):
    """A transcript that cannot be parsed must read as "not a completion claim"
    so dispatch degrades to the existing resume behavior instead of crashing."""
    bad = tmp_path / ".agent_transcript.json"
    bad.write_text("{not json at all")
    assert pd._transcript_ends_with_done(bad) is False

    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert pd._transcript_ends_with_done(empty) is False

    missing = tmp_path / "absent.json"
    assert pd._transcript_ends_with_done(missing) is False

    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text('{"messages": []}')
    assert pd._transcript_ends_with_done(not_a_list) is False
