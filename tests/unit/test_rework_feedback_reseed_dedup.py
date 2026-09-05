"""Tests for deduping review-feedback re-injection on the local-backend
transcript-resume rework path (pipeline/dispatch.py).

Measured live (2026-09-04, real .agent_transcript.json): each rework cycle
appends the FULL "Original review feedback (for reference):" text into the
SAME resumed transcript — ~7.2-7.8K chars re-injected per round, ~22K chars
across 3 rounds of one story. When the feedback is byte-identical to what a
prior cycle already put in the transcript, the new cycle must reference it
instead of re-embedding it. Any failure reading/parsing the transcript must
fail OPEN to today's re-injection behavior (a token optimization must never
break dispatch).
"""
import json

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage

FEEDBACK = (
    "The SQL is injectable; parameterize it. Also the retry loop swallows "
    "exceptions and the test fixture leaks the fake transport across tests."
)
CHECKLIST = "1. Parameterize the SQL query.\n2. Add a regression test for the retry loop."


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


@pytest.fixture(autouse=True)
def _isolate_usage_state(tmp_path, monkeypatch):
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    return path


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                transcript_messages, plan_name="fbreseed", checklist=None,
                transcript_raw=None):
    """Drive dispatch_story through the local-backend transcript-resume rework
    path with the given prior transcript; return LOCAL_AGENT_RESUME_APPEND_CONTENT."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".agent_transcript.json").write_text(
        transcript_raw if transcript_raw is not None else json.dumps(transcript_messages)
    )
    _write_manifest(plan_dir, plan_name, {"S1": {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "_dispatched_agent_instructions": "Build it.",
        "status": "changes_requested", "worktree": str(worktree_path),
        "review_feedback": FEEDBACK,
    }})
    monkeypatch.setattr(
        p, "_run_rework_planner", (lambda *a, **k: checklist) if checklist else lambda *a, **k: None
    )

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7100)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story(plan_name, "S1")
    assert popen_calls, "dispatch_story did not invoke the local backend"
    return popen_calls[0]["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"]


_PRIOR_CYCLE_MSGS = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": f"The code reviewer REQUESTED CHANGES on your previous attempt. Address this feedback:\n{FEEDBACK}"},
]


def test_feedback_already_in_transcript_is_referenced_not_reinjected(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    append = _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch, _PRIOR_CYCLE_MSGS)
    assert FEEDBACK not in append
    assert "already in your transcript" in append


def test_feedback_not_in_transcript_is_still_injected_in_full(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Contract guard: a transcript that does NOT contain the feedback keeps
    today's byte-for-byte re-injection (first rework cycle after a cold start)."""
    append = _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                         [{"role": "system", "content": "sys"}])
    assert f"Address this feedback:\n{FEEDBACK}" in append


def test_feedback_dedup_fails_open_on_unparseable_transcript(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A corrupt/unparseable transcript must fail OPEN: re-inject the feedback
    (today's behavior) rather than break or degrade the dispatch."""
    append = _rework_env(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        None, plan_name="fbreseedcorrupt", transcript_raw="{not valid json",
    )
    assert f"Address this feedback:\n{FEEDBACK}" in append


def test_feedback_dedup_applies_to_checklist_branch_too(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    append = _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                         _PRIOR_CYCLE_MSGS, checklist=CHECKLIST)
    assert CHECKLIST in append          # the freshly-generated checklist stays
    assert FEEDBACK not in append       # the verbatim repeat goes
    assert "already in your transcript" in append


def test_feedback_checklist_branch_keeps_full_feedback_when_not_in_transcript(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    append = _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                         [{"role": "system", "content": "sys"}], checklist=CHECKLIST)
    assert CHECKLIST in append
    assert f"Original review feedback (for reference):\n{FEEDBACK}" in append


def test_feedback_dedup_handles_non_string_content_entries(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Transcript messages with list/None content must not crash the check."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": [{"type": "text", "text": FEEDBACK}]},
    ]
    append = _rework_env(plan_dir, worktree_root, agents_dir, monkeypatch, msgs)
    # List-form content is not the seeded user-turn form; fail closed to
    # re-injection is acceptable — the point is no crash and correct behavior
    # for the string case.
    assert "Address this feedback" in append or "already in your transcript" in append