"""Contract for the per-attempt ``TRACEPARENT`` handed to the agent process.

``pipeline.dispatch`` stages ``PIPELINE_CORRELATION_ID`` in ``os.environ`` for
exactly the synchronous launch call, because the backend drivers build the
agent's environment from ``os.environ`` at launch time. The same staging hands
the subprocess a W3C ``TRACEPARENT`` whose trace id is the attempt trace the
lifecycle sink files the ``agent_dispatched`` span under, so the agent's own
spans join the trace of the dispatch attempt that started it.

The staged value is derived only from a validated story correlation id, so an
unusable id stages nothing at all -- it must not delete or overwrite a
``TRACEPARENT`` the surrounding process already had.

``import pipeline.server`` comes first: a pipeline module imported cold without
it hits the pre-existing import cycle (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``). Fixtures are copied from
``tests/unit/test_w4l_dispatch_correlation.py`` per this repo's convention --
there is no shared conftest.py for these.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess

import pytest

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first
from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import telemetry, trace_context
from pipeline import ticketing as pt
from pipeline.trace_context import attempt_trace_id, parse_traceparent

pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

CID = "c0ffee1234ab"
PLAN = "obs-trace-context"
SENTINEL = "00-11111111111111111111111111111111-2222222222222222-01"
ENDPOINT_ENVS = (
    "PIPELINE_OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


def _agent_traceparent(*args, **kwargs):
    """Fetch the unit under test, failing on an assertion not an ImportError."""
    fn = getattr(trace_context, "agent_traceparent", None)
    assert fn is not None, "pipeline.trace_context must expose agent_traceparent()"
    return fn(*args, **kwargs)


# ---------- Fixtures (copied from test_w4l_dispatch_correlation.py) ----------
@pytest.fixture(autouse=True)
def _clean_telemetry(monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    telemetry.reset()
    yield
    telemetry.reset()


@pytest.fixture
def exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    memory = InMemorySpanExporter()
    telemetry.configure(memory)
    return memory


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path):
    """A throwaway origin + clone so dispatch's git fetch/worktree add run for
    real without touching any remote."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("x\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    return origin, repo


def _make_story(**overrides):
    story = {
        "summary": "Do the thing",
        "agent_instructions": "Build it.",
        "status": "pending",
        "dependencies": [],
        "role": "software-engineer",
        "backend": "local",
        "model": "test-model",
        "correlation_id": CID,
        "dispatch_attempts": 0,
    }
    story.update(overrides)
    return story


class _FakeHandle:
    pid = 4242
    model = "test-model"


class _RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def subscribe(self, *_a, **_k):
        return None


class _LaunchRecorder:
    """Fake dispatch backend: records the env the launch call observes."""

    def __init__(self):
        self.launch_envs = []
        self.raise_on_dispatch = False
        self.bus = _RecordingBus()

    def get_backend(self, *_args, **_kwargs):
        return self

    def dispatch(self, **_kwargs):
        self.launch_envs.append(dict(os.environ))
        if self.raise_on_dispatch:
            raise RuntimeError("launch failed")
        return _FakeHandle()

    def staged_traceparents(self):
        return [env.get("TRACEPARENT") for env in self.launch_envs]

    def events_of_type(self, event_type):
        return [e for e in self.bus.events if e.get("type") == event_type]


@pytest.fixture
def recorder(monkeypatch):
    rec = _LaunchRecorder()
    monkeypatch.setattr(backend, "get_backend", rec.get_backend)
    from pipeline import event_wiring

    monkeypatch.setattr(event_wiring, "get_bus", lambda: rec.bus)
    return rec


def _no_plane(*_a, **_k):
    return (_ for _ in ()).throw(RuntimeError("no plane"))


@pytest.fixture
def plan(tmp_path, plan_dir, worktree_root, agents_dir, recorder, monkeypatch):
    """A one-story plan on a real throwaway repo; returns (plan_name, story)."""
    _origin, repo = _make_repo(tmp_path)
    stories = {"S1": _make_story()}
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories, "repo_root": str(repo)})
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)
    return PLAN


def _dispatch(plan_name):
    result = p.dispatch_story(plan_name, "S1")
    assert result.get("ok") is True, f"dispatch failed: {result}"
    return result


# ---------- The staged header -----------------------------------------------
def test_launch_sees_traceparent_for_attempt_zero(plan, recorder):
    _dispatch(plan)

    staged = recorder.staged_traceparents()
    assert staged, "the dispatch backend ran, so a launch env was recorded"
    traceparent = staged[0]
    assert traceparent == _agent_traceparent(CID, 0), (
        "the launch call must see the attempt-0 traceparent for the story"
    )
    parsed = parse_traceparent(traceparent)
    assert parsed is not None, "the staged header must be a valid W3C traceparent"
    assert parsed["trace_id"] == attempt_trace_id(CID, 0)


def test_launch_traceparent_tracks_non_zero_attempt(tmp_path, plan_dir,
                                                   worktree_root, agents_dir,
                                                   recorder, monkeypatch):
    _origin, repo = _make_repo(tmp_path)
    stories = {"S1": _make_story(dispatch_attempts=2)}
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories, "repo_root": str(repo)})
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)

    _dispatch(PLAN)

    assert recorder.staged_traceparents()[0] == _agent_traceparent(CID, 2), (
        "the staged traceparent must use the story's dispatch_attempts, not a "
        "hard-coded attempt 0"
    )


def test_staged_traceparent_matches_agent_dispatched_lifecycle_span(
    plan, recorder, exporter
):
    _dispatch(plan)

    staged = parse_traceparent(recorder.staged_traceparents()[0])
    assert staged is not None
    events = recorder.events_of_type("agent_dispatched")
    assert len(events) == 1, "dispatch publishes exactly one agent_dispatched event"
    attempt = events[0]["payload"]["attempt"]

    assert staged["trace_id"] == attempt_trace_id(CID, attempt)

    telemetry.record_lifecycle_event(events[0])
    spans = exporter.get_finished_spans()
    assert spans, "the agent_dispatched lifecycle event must emit a span"
    assert format(spans[-1].context.trace_id, "032x") == staged["trace_id"], (
        "the agent launch and its agent_dispatched span must share one trace id"
    )


# ---------- Restoring the surrounding environment ---------------------------
def test_previous_traceparent_is_restored(plan, recorder, monkeypatch):
    monkeypatch.setenv("TRACEPARENT", SENTINEL)

    _dispatch(plan)

    assert recorder.staged_traceparents()[0] != SENTINEL
    assert os.environ.get("TRACEPARENT") == SENTINEL, (
        "the pre-existing TRACEPARENT must be restored after the launch call"
    )


def test_no_traceparent_afterwards_when_there_was_none(plan, recorder, monkeypatch):
    monkeypatch.delenv("TRACEPARENT", raising=False)

    _dispatch(plan)

    assert recorder.staged_traceparents()[0]
    assert "TRACEPARENT" not in os.environ, (
        "a staged TRACEPARENT must not leak past the launch call"
    )


def test_previous_traceparent_is_restored_when_launch_raises(plan, recorder,
                                                             monkeypatch):
    monkeypatch.setenv("TRACEPARENT", SENTINEL)
    recorder.raise_on_dispatch = True

    with contextlib.suppress(Exception):
        p.dispatch_story(PLAN, "S1")

    assert os.environ.get("TRACEPARENT") == SENTINEL, (
        "a launch failure must still restore the pre-existing TRACEPARENT"
    )


# ---------- Negative: unusable correlation ids stage nothing ----------------
def test_unusable_correlation_id_stages_nothing(tmp_path, plan_dir, worktree_root,
                                                agents_dir, recorder, monkeypatch):
    _origin, repo = _make_repo(tmp_path)
    stories = {"S1": _make_story(correlation_id="bad id")}
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories, "repo_root": str(repo)})
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)
    monkeypatch.setenv("TRACEPARENT", SENTINEL)

    _dispatch(PLAN)

    assert recorder.staged_traceparents() == [SENTINEL], (
        "an id agent_traceparent rejects must leave the surrounding "
        "TRACEPARENT untouched"
    )
    assert os.environ.get("TRACEPARENT") == SENTINEL


# ---------- Negative: agent_traceparent input validation --------------------
def test_agent_traceparent_rejects_invalid_inputs():
    assert _agent_traceparent("bad id", 0) is None
    assert _agent_traceparent("abc", -1) is None
    assert _agent_traceparent("abc", True) is None
    assert _agent_traceparent(None, 0) is None
    assert _agent_traceparent("", 0) is None
    assert _agent_traceparent("a" * 65, 0) is None


def test_agent_traceparent_accepts_a_valid_id():
    traceparent = _agent_traceparent("corr-1", 0)
    parsed = parse_traceparent(traceparent)
    assert parsed is not None
    assert parsed["trace_id"] == attempt_trace_id("corr-1", 0)
    assert parsed["flags"] == "01"
