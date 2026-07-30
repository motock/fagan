"""Acceptance fixture for wiring a rework-cycle test-author phase into
dispatch_story (pipeline/planner.py + pipeline/server.py).

Exercises the REAL dispatch_story rework path (not the new helper
functions in isolation): when a rework's review feedback requires new test
case(s), the wiring must invoke a rework test-author phase BEFORE the main
executor redispatch and steer the executor prompt to not touch tests; when
it does not require new tests, the phase is already done for this sha, or
the phase fails/is unconfigured, behavior must be byte-for-byte identical
to today (fail-open).

Imports shared fixtures/helpers from test_pipeline_mcp_server.py rather
than redefining them, mirroring the existing rework-dispatch tests
(test_dispatch_story_local_rework_resumes_transcript_when_present) and the
existing TDD-split wiring tests
(test_dispatch_story_tdd_split_opted_in_runs_phase_and_augments_prompt) -
both in that file - which this fixture directly extends one rework cycle
later.
"""

import json

import pytest
from test_pipeline_mcp_server import (
    _FakeProc,
    _read_manifest,
    _write_manifest,
)

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt


# ---------- Fixtures (mirror test_pipeline_mcp_server.py / test_tdd_split_
# always_on.py locally rather than importing them, so the fixture name and
# the test-function parameter don't shadow each other (ruff F811). ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper

    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

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


def _mock_common(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


def _base_story(worktree_path):
    return {
        "S1": {
            "summary": "Do thing",
            "agent_instructions": "Build it.",
            "status": "changes_requested",
            "worktree": str(worktree_path),
            "review_feedback": "The `since` comparison crashes on a naive timestamp.",
            "last_reviewed_sha": "deadbeef",
        },
    }


def _setup_worktree(worktree_root):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(
        json.dumps(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "task"},
            ]
        )
    )
    return worktree_path


def test_rework_dispatch_runs_test_author_phase_when_new_tests_required(
    plan_dir,
    worktree_root,
    agents_dir,
    monkeypatch,
):
    """When _rework_requires_new_tests signals True, dispatch_story must
    run _run_rework_test_author_phase BEFORE the main executor redispatch,
    mark rework_test_author_done_for_sha, and steer the resumed prompt to
    not touch tests."""
    _mock_common(monkeypatch)
    worktree_path = _setup_worktree(worktree_root)
    _write_manifest(plan_dir, "rwta1", _base_story(worktree_path))

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rework_requires_new_tests", lambda *a, **k: True)

    phase_calls = []

    def _fake_phase(
        story,
        *,
        story_key,
        worktree_path,
        dispatch_backend,
        local_model,
        review_feedback,
        fix_checklist=None,
        plan_role_config=None,
        **kwargs,
    ):
        phase_calls.append(
            {
                "story_key": story_key,
                "worktree_path": worktree_path,
                "dispatch_backend": dispatch_backend,
                "review_feedback": review_feedback,
            }
        )
        return True

    monkeypatch.setattr(p, "_run_rework_test_author_phase", _fake_phase)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7001)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)

    result = p.dispatch_story("rwta1", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1
    assert phase_calls[0]["story_key"] == "S1"
    assert phase_calls[0]["worktree_path"] == worktree_path
    assert phase_calls[0]["dispatch_backend"] == "local"
    assert "naive timestamp" in phase_calls[0]["review_feedback"]

    env = popen_calls[0]["env"]
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "already been written and committed" in append
    assert p._NEVER_TOUCH_TESTS_STEERING in append

    manifest = _read_manifest(plan_dir, "rwta1")
    assert manifest["stories"]["S1"]["rework_test_author_done_for_sha"] == "deadbeef"


def test_rework_dispatch_skips_test_author_phase_when_not_required(
    plan_dir,
    worktree_root,
    agents_dir,
    monkeypatch,
):
    """When _rework_requires_new_tests signals False, the phase must NOT
    run and the resumed prompt must be byte-for-byte identical to today's
    unmodified feedback-only format - the core fail-open/no-regression
    contract."""
    _mock_common(monkeypatch)
    worktree_path = _setup_worktree(worktree_root)
    _write_manifest(plan_dir, "rwta2", _base_story(worktree_path))

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rework_requires_new_tests", lambda *a, **k: False)

    def _boom(*a, **k):
        raise AssertionError("rework test-author phase must not run when not required")

    monkeypatch.setattr(p, "_run_rework_test_author_phase", _boom)

    popen_calls = []
    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda cmd, env, **kw: (
            popen_calls.append({"cmd": cmd, "env": env}) or _FakeProc(7002)
        ),
    )

    p.dispatch_story("rwta2", "S1")

    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\n"
        "The `since` comparison crashes on a naive timestamp."
    )
    manifest = _read_manifest(plan_dir, "rwta2")
    assert "rework_test_author_done_for_sha" not in manifest["stories"]["S1"]


def test_rework_dispatch_skips_test_author_phase_when_already_done_for_sha(
    plan_dir,
    worktree_root,
    agents_dir,
    monkeypatch,
):
    """A retried/resumed dispatch on the SAME review cycle (same
    last_reviewed_sha) must not re-run the rework test-author phase, or
    even re-ask _rework_requires_new_tests, a second time."""
    _mock_common(monkeypatch)
    worktree_path = _setup_worktree(worktree_root)
    story = _base_story(worktree_path)
    story["S1"]["rework_test_author_done_for_sha"] = "deadbeef"
    _write_manifest(plan_dir, "rwta3", story)

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    def _must_not_ask(*a, **k):
        raise AssertionError("must not even be asked again for the same sha")

    def _must_not_run(*a, **k):
        raise AssertionError("must not re-run for the same sha")

    monkeypatch.setattr(p, "_rework_requires_new_tests", _must_not_ask)
    monkeypatch.setattr(p, "_run_rework_test_author_phase", _must_not_run)

    popen_calls = []
    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda cmd, env, **kw: (
            popen_calls.append({"cmd": cmd, "env": env}) or _FakeProc(7003)
        ),
    )

    p.dispatch_story("rwta3", "S1")

    env = popen_calls[0]["env"]
    assert (
        "already been written and committed"
        not in env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    )


def test_rework_test_author_phase_failure_falls_open(
    plan_dir,
    worktree_root,
    agents_dir,
    monkeypatch,
):
    """A rework test-author phase that fails (unconfigured role, dispatch
    error, timeout, or no new commit -> returns False) must leave no
    marker and must NOT alter the resumed prompt - the rework proceeds
    exactly as if new tests were never required."""
    _mock_common(monkeypatch)
    worktree_path = _setup_worktree(worktree_root)
    _write_manifest(plan_dir, "rwta4", _base_story(worktree_path))

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rework_requires_new_tests", lambda *a, **k: True)
    monkeypatch.setattr(p, "_run_rework_test_author_phase", lambda *a, **k: False)

    popen_calls = []
    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda cmd, env, **kw: (
            popen_calls.append({"cmd": cmd, "env": env}) or _FakeProc(7004)
        ),
    )

    p.dispatch_story("rwta4", "S1")

    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\n"
        "The `since` comparison crashes on a naive timestamp."
    )
    manifest = _read_manifest(plan_dir, "rwta4")
    assert "rework_test_author_done_for_sha" not in manifest["stories"]["S1"]
