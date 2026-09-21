"""Acceptance: the dispatch-time baseline's failing node ids must reach the
agent-side full-suite done-gate.

The tick-side grade exempts failures that already failed in the pre-dispatch
baseline snapshot (`_baseline_exempted_failures`). The agent-side gate has no
such exemption, so a rework round whose repo-wide suite carries pre-existing,
unrelated failures can never go green: it keeps rejecting `done` until the
suite-reject cap parks it, even though the tick grading that same round
exempts exactly those failures.

Two things have to hold for that exemption to be possible at all:
  1. the parser the gate needs (`pipeline.build_detect.failed_node_ids`) must
     be reachable through the server handle the agent modules hold as their
     ``origin["p"]`` - they deliberately import only the stdlib;
  2. the dispatch-time snapshot must persist the ids INTO the worktree, since
     the agent subprocess never sees the story dict. It writes them into the
     existing, already-git-excluded ``.dispatch_baseline_test_checked`` marker.

A snapshot that reports no ids (a passing baseline, or one whose command
printed no pytest summary) must still record an empty list rather than
crashing or omitting the key - an unreadable baseline must exempt nothing.

Fixtures are resolved by name via ``request.getfixturevalue`` rather than by
same-named test parameters: this file is a digest-pinned read-only oracle, so
it must not need a pyproject per-file-ignore to lint clean.
"""
import json

from app import backend
from pipeline import build_detect as bd
from pipeline import dispatch
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

_MARKER = ".dispatch_baseline_test_checked"
_PLAN = "baselineids"


class _CapturingDispatchBackend:
    """Captures the kwargs _dispatch_story_impl hands to dispatch()."""

    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeProc(4242)

    def complete(self, prompt, **kwargs):
        return ""


def _stub_fresh_dispatch(monkeypatch, worktree_path):
    """Stub every external boundary a FRESH (non-resuming) local dispatch
    touches, and simulate ``git worktree add`` creating the worktree dir."""
    def _fake_run(cmd, **kw):
        if isinstance(cmd, (list, tuple)) and list(cmd)[:3] == ["git", "worktree", "add"]:
            worktree_path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        backend.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc(9500)
    )
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_provision_worktree_venv", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)
    capturing = _CapturingDispatchBackend()
    monkeypatch.setattr(backend, "get_backend", lambda role, name=None: capturing)
    return capturing


def _dispatch_with_baseline(monkeypatch, request, snapshot):
    """Drive a real fresh local dispatch whose baseline snapshot is `snapshot`;
    return the worktree the marker was written into."""
    plans = request.getfixturevalue("plan_dir")
    roots = request.getfixturevalue("worktree_root")
    request.getfixturevalue("agents_dir")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = roots / "S1"
    _write_manifest(plans, _PLAN, {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    _stub_fresh_dispatch(monkeypatch, worktree_path)
    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", lambda path: snapshot)
    p.dispatch_story(_PLAN, "S1")
    return worktree_path


def _marker_payload(worktree_path):
    return json.loads((worktree_path / _MARKER).read_text(encoding="utf-8"))


def test_marker_carries_the_baselines_failed_node_ids(monkeypatch, request):
    """The whole point: a failing baseline's node ids land in the worktree, so
    the agent-side gate can compare a later red run against them."""
    wt = _dispatch_with_baseline(monkeypatch, request, {
        "cmd": ["pytest", "-q"],
        "returncode": 1,
        "stdout_tail": "",
        "stderr_tail": "",
        "failed_node_ids": ["tests/a.py::test_x", "tests/b.py::test_y"],
    })
    assert _marker_payload(wt)["failed_node_ids"] == [
        "tests/a.py::test_x", "tests/b.py::test_y",
    ]


def test_marker_records_no_ids_when_the_snapshot_omits_them(monkeypatch, request):
    """A snapshot dict without the key (or an empty one) must record an empty
    list, not raise - the write is inside the dispatch path."""
    wt = _dispatch_with_baseline(monkeypatch, request, {
        "cmd": ["pytest", "-q"],
        "returncode": 1,
        "stdout_tail": "",
        "stderr_tail": "",
    })
    assert _marker_payload(wt)["failed_node_ids"] == []


def test_marker_records_no_ids_for_a_passing_baseline(monkeypatch, request):
    """A green baseline exempts nothing: every current failure is the story's."""
    wt = _dispatch_with_baseline(monkeypatch, request, {
        "cmd": ["pytest", "-q"],
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "failed_node_ids": [],
    })
    assert _marker_payload(wt)["failed_node_ids"] == []


def test_failed_node_ids_is_reachable_on_the_agents_server_handle():
    """The agent modules hold ``pipeline.server``'s surface as ``origin["p"]``
    and import nothing from the pipeline package themselves, so the parser has
    to be part of that re-exported surface."""
    assert p.failed_node_ids is bd.failed_node_ids
    assert p.failed_node_ids("FAILED tests/a.py::test_x - assert False\n") == [
        "tests/a.py::test_x"
    ]
