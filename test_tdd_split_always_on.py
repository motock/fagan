"""TDD-split (test-author phase) is ALWAYS-ON for stories that opt in.

These tests verify the removal of the global ``PIPELINE_TDD_SPLIT`` on/off
toggle (TDD_SPLIT_PRODUCTION_PLAN.md §4): the test-author phase now runs for
any story that opts in via the per-story ``tdd_split`` field, is not a
resuming/rework redispatch, and has no pre-existing test-author marker in the
worktree -- with NO global env-var gate. The safety nets (same-model
refusal, per-story opt-in, not-resuming guard, marker check) all survive the
flag removal.

These tests are RED against the as-shipped code that still gates on
``os.environ.get('PIPELINE_TDD_SPLIT', 'off') == 'on'``: with the env var
UNSET the current gate short-circuits to "off" and skips the phase, so the
"phase RUNS" assertions fail. They go GREEN once the ``tdd_split_mode``
conjunct is deleted from ``pipeline/server.py``.

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
    """The whole point of this feature change: PIPELINE_TDD_SPLIT is GONE.
    Every test in this module runs with the var explicitly UNSET so we
    exercise the always-on path, never the legacy on/off toggle."""
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


# ---------- (a) UNSET + opted-in + non-resuming + distinct role => RUNS ----------
def test_tdd_split_unset_opted_in_non_resuming_runs_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With PIPELINE_TDD_SPLIT UNSET, an opted-in (tdd_split=true),
    non-resuming story with a distinct test_author role RUNS the
    test-author phase. The global on/off toggle is gone; the per-story
    tdd_split field is the only opt-in."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdrun", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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
    assert len(phase_calls) == 1, "test-author phase MUST run with the var unset"
    assert phase_calls[0]["story_key"] == "S1"
    assert phase_calls[0]["worktree_path"] == worktree_root / "S1"
    assert phase_calls[0]["dispatch_backend"] == "local"

    marker = worktree_root / "S1" / ".tdd_split_test_author_done"
    assert marker.exists(), "a successful phase must write the marker"

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


# ---------- (b) same-model refusal still skips with var unset ----------
def test_tdd_split_unset_same_model_refusal_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The same-model safety net (§2.2) survives the flag removal: even with
    PIPELINE_TDD_SPLIT unset and the story opted in, if the test_author role
    resolves to the SAME backend+model dispatch is already using,
    _resolve_test_author_backend returns (None, None) and the phase skips
    safely (fail-open, no marker, executor prompt un-augmented)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdsame", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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


# ---------- (c) tdd_split absent/false still skips ----------
def test_tdd_split_unset_story_not_opted_in_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """tdd_split absent (defaults to False) must skip the phase even with
    everything else enabled and the var unset. The per-story opt-in is the
    sole gate; the global toggle's removal does not make the split
    unconditional for every story."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdnoopt", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},  # no tdd_split
    })

    def _boom(*a, **k):
        raise AssertionError(
            "phase must not run without the per-story tdd_split opt-in")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    popen_calls = _wire_dispatch_no_plane(monkeypatch, pid=9203)

    result = p.dispatch_story("tdnoopt", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task


def test_tdd_split_unset_story_explicitly_false_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An explicit tdd_split=False must skip the phase too (boundary: the
    falsy value, not just absence)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdfalse", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": False},
    })

    def _boom(*a, **k):
        raise AssertionError(
            "phase must not run when tdd_split is explicitly False")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    _wire_dispatch_no_plane(monkeypatch, pid=9204)

    result = p.dispatch_story("tdfalse", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()


# ---------- (d) a resuming dispatch still skips ----------
def test_tdd_split_unset_resuming_dispatch_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework/interrupted redispatch (status in interrupted/
    changes_requested) must NOT get a fresh test-authoring pass: reworks act
    on the SAME committed tests. The not-resuming guard survives the flag
    removal."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdresume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "dependencies": [], "tdd_split": True},
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
def test_tdd_split_unset_marker_present_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A worktree that already has a .tdd_split_test_author_done marker must
    not re-run the phase (belt-and-suspenders with `resuming`). The marker
    check survives the flag removal."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdmarker", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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
def test_tdd_split_unset_role_unconfigured_skips_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """If the test_author role is unconfigured (registry has no test_author
    entry and no env override), _resolve_test_author_backend returns
    (None, None) and the phase skips safely -- the split is a bonus, never a
    gate (§2.5). Survives the flag removal."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    _write_manifest(plan_dir, "tdunconf", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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
    identical to it being unset -- the per-story opt-in alone decides. This
    test documents that the env var is now a dead letter (harmless no-op),
    not a re-introduced gate. An opted-in non-resuming story runs the phase
    regardless of the var's value."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_TDD_SPLIT", "on")  # stale; must be ignored
    _write_manifest(plan_dir, "tdstale", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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
    NOT suppress the phase for an opted-in non-resuming story. The var is
    gone; 'off' is no longer a meaningful signal."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_TDD_SPLIT", "off")  # stale; must be ignored
    _write_manifest(plan_dir, "tdstaleoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
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
        "PIPELINE_TDD_SPLIT=off must no longer suppress an opted-in phase"
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()