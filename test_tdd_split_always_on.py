"""TDD-split (test-author phase) is ALWAYS-ON for every local-family dispatch.

These tests verify the full removal of both TDD-split gates: the global
``PIPELINE_TDD_SPLIT`` on/off toggle (removed in an earlier change) and the
per-story ``tdd_split`` opt-in field (removed here). The test-author phase
now runs unconditionally for local-family dispatch, mirroring exactly how the
guided-decomposition planner became always-on: gated only on

  - a local-family backend (``dispatch_backend in _LOCAL_BACKEND_NAMES``),
  - not a resuming/rework redispatch, and
  - no pre-existing test-author marker in the worktree.

The remaining safety nets (same-model refusal, not-resuming guard, marker
check, fail-open on an unconfigured role) all survive both flag removals.

These tests are RED against code that still gates on ``story["tdd_split"]``:
with the field absent (or explicitly False) the old gate short-circuits and
skips the phase, so the "phase RUNS regardless of the field" assertions
fail. They go GREEN once the ``story.get("tdd_split")`` conjunct is replaced
with ``dispatch_backend in _LOCAL_BACKEND_NAMES`` in ``pipeline/server.py``.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q test_tdd_split_always_on.py
"""

import json

import pytest

import backend
from pipeline import server as p
from pipeline import ticketing as pt
import role_registry


# ---------- Fixtures (mirror test_pipeline_mcp_server.py) ----------
@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


@pytest.fixture(autouse=True)
def _tdd_split_env_unset(monkeypatch):
    """PIPELINE_TDD_SPLIT is GONE. Every test in this module runs with the
    var explicitly UNSET so we exercise the always-on path, never the
    legacy on/off toggle."""
    monkeypatch.delenv("PIPELINE_TDD_SPLIT", raising=False)


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


@pytest.fixture(autouse=True)
def _isolate_usage_state(tmp_path, monkeypatch):
    from pipeline import usage as pusage

    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    return path


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _wire_dispatch_no_plane(monkeypatch, pid=9201):
    """Wire the minimum mocks dispatch_story needs to reach the executor
    Popen without touching git/Plane/a real backend: subprocess.run is a
    no-op, the local backend's Popen returns a fake proc, plane_request
    raises (we never want a real HTTP call), and _default_branch is fixed."""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(pid)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    return popen_calls


# ---------- (a) local dispatch, no tdd_split field at all => RUNS ----------
def test_tdd_split_runs_for_local_dispatch_field_absent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A non-resuming local-family dispatch runs the test-author phase even
    though the story has no ``tdd_split`` field at all. The per-story
    opt-in is gone; every story goes through the split now."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdrun", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},  # no tdd_split key
    })

    phase_calls = []

    def _fake_phase(story, *, story_key, worktree_path, dispatch_backend,
                    local_model, plan_role_config=None, **kwargs):
        phase_calls.append({
            "story_key": story_key, "worktree_path": worktree_path,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
            "plan_role_config": plan_role_config,
        })
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_phase)
    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9201)

    result = p.dispatch_story("tdrun", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1, "test-author phase MUST run with no opt-in field"
    assert phase_calls[0]["story_key"] == "S1"
    assert phase_calls[0]["worktree_path"] == worktree_root / "S1"
    assert phase_calls[0]["dispatch_backend"] == "local"

    marker = worktree_root / "S1" / ".tdd_split_test_author_done"
    assert marker.exists(), "a successful phase must write the marker"

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


# ---------- (a2) explicit tdd_split=False no longer suppresses the phase ----------
def test_tdd_split_runs_even_when_field_explicitly_false(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Boundary: an explicit ``tdd_split: False`` on the story must NOT
    suppress the phase either -- the field is fully inert now, not just
    "defaults to on". There is no per-story escape hatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdfalse", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": False},
    })

    phase_calls = []

    def _fake_phase(story, *, story_key, worktree_path, dispatch_backend,
                    local_model, plan_role_config=None, **kwargs):
        phase_calls.append({"story_key": story_key})
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_phase)
    _wire_dispatch_no_plane(monkeypatch, pid=9204)

    result = p.dispatch_story("tdfalse", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1, \
        "tdd_split=False must no longer suppress the phase"
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()


# ---------- (b) a Claude dispatch skips the phase regardless ----------
def test_tdd_split_claude_backend_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The split is a crutch for the weak local executor only -- a Claude
    dispatch must never trigger test-authoring even though the phase is now
    always-on for local-family backends. Mirrors the planner's identical
    Claude-skip behavior."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)  # default -> claude
    _write_manifest(plan_dir, "tdclaude", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("phase must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    def _fake_claude_popen(cmd, **kw):
        return _FakeProc(9210)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_claude_popen)

    result = p.dispatch_story("tdclaude", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()


# ---------- (c) same-model refusal still skips ----------
def test_tdd_split_same_model_refusal_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The same-model safety net survives both flag removals: if the
    test_author role resolves to the SAME backend+model dispatch is already
    using, _resolve_test_author_backend returns (None, None) and the phase
    skips safely (fail-open, no marker, executor prompt un-augmented)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdsame", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    # Force the real _run_test_author_phase down the refusal path: the role
    # resolver returns (None, None) -> phase returns False before any
    # dispatch is attempted. This exercises the genuine safety net, not a
    # mocked phase result.
    monkeypatch.setattr(p, "_resolve_test_author_backend",
                        lambda *a, **k: (None, None))
    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9202)

    result = p.dispatch_story("tdsame", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists(), \
        "same-model refusal must leave no marker"
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task, \
        "a refused phase must not augment the executor prompt"


# ---------- (d) a resuming dispatch still skips ----------
def test_tdd_split_resuming_dispatch_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework/interrupted redispatch (status in interrupted/
    changes_requested) must NOT get a fresh test-authoring pass: reworks act
    on the SAME committed tests. The not-resuming guard survives both flag
    removals."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdresume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError(
            "phase must not run on a resuming/rework dispatch")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9205)

    result = p.dispatch_story("tdresume", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task


# ---------- (e) test_author_marker.exists() still skips ----------
def test_tdd_split_marker_present_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A worktree that already has a .tdd_split_test_author_done marker must
    not re-run the phase (belt-and-suspenders with `resuming`). The marker
    check survives both flag removals."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdmarker", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".tdd_split_test_author_done").write_text("ok\n")

    def _boom(*a, **k):
        raise AssertionError(
            "phase must not re-run when the test-author marker already exists")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9206)

    result = p.dispatch_story("tdmarker", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task, \
        "an existing marker must still seed the executor with the committed tests"


# ---------- boundary: test_author role unconfigured => phase skips (None,None) ----------
def test_tdd_split_role_unconfigured_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """If the test_author role is unconfigured (registry has no test_author
    entry and no env override), _resolve_test_author_backend returns
    (None, None) and the phase skips safely -- the split is a bonus, never a
    gate (§2.5). Survives both flag removals."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    _write_manifest(plan_dir, "tdunconf", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9207)

    result = p.dispatch_story("tdunconf", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists(), \
        "an unconfigured test_author role must leave no marker"
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task


# ---------- the global toggle is gone: setting it must NOT re-enable a gate ----------
def test_tdd_split_env_var_is_now_ignored_when_set_on(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard for the flag removal: even if a stale operator
    environment still exports PIPELINE_TDD_SPLIT=on, the behavior is
    identical to it being unset -- local-family dispatch alone decides. This
    documents that the env var is a dead letter (harmless no-op), not a
    re-introduced gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_TDD_SPLIT", "on")  # stale; must be ignored
    _write_manifest(plan_dir, "tdstale", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    phase_calls = []

    def _fake_phase(story, *, story_key, worktree_path, dispatch_backend,
                    local_model, plan_role_config=None, **kwargs):
        phase_calls.append({"story_key": story_key})
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_phase)
    _wire_dispatch_no_plane(monkeypatch, pid=9208)

    result = p.dispatch_story("tdstale", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()


def test_tdd_split_env_var_is_now_ignored_when_set_off(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Symmetric guard: PIPELINE_TDD_SPLIT=off (the old "disable" value) must
    NOT suppress the phase for a local-family dispatch. The var is gone;
    'off' is no longer a meaningful signal."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_TDD_SPLIT", "off")  # stale; must be ignored
    _write_manifest(plan_dir, "tdstaleoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    phase_calls = []

    def _fake_phase(story, *, story_key, worktree_path, dispatch_backend,
                    local_model, plan_role_config=None, **kwargs):
        phase_calls.append({"story_key": story_key})
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_phase)
    _wire_dispatch_no_plane(monkeypatch, pid=9209)

    result = p.dispatch_story("tdstaleoff", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1, \
        "PIPELINE_TDD_SPLIT=off must no longer suppress the phase"
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
