"""Tests for the pipeline MCP server: the remaining decompose-role tests, test_author role resolution, and dispatch_story wiring for the TDD-split test-author phase.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import hashlib
import json
import subprocess

from app import backend
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


def test_decompose_plan_malformed_json_fails_with_raw_text_preserved(
    agents_dir, monkeypatch,
):
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: "not json at all")

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "raw" in result
    assert result["raw"] == "not json at all"


def test_decompose_plan_rejects_json_missing_epics_list(agents_dir, monkeypatch):
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: '{"not_epics": []}')

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "epics" in result["error"]


def test_decompose_plan_fails_open_when_backend_returns_none(agents_dir, monkeypatch):
    """_run_decompose already fails open (returns None) on a broken backend -
    decompose_plan must surface that as ok=False, never raise."""
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: None)

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False


def test_dispatch_story_decompose_rework_translates_feedback_into_fix_checklist(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch that resumes via transcript (always-on for
    local-family dispatch) must translate the raw review feedback into a
    tech-lead fix checklist (via _run_rework_planner) before appending it -
    not pasted verbatim, mirroring the initial-dispatch checklist treatment."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "dcrework", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "allow() double-counts refill on every call."},
    })

    rework_calls = []

    def _fake_rework_planner(review_feedback, *, dispatch_backend, local_model, **k):
        rework_calls.append({
            "review_feedback": review_feedback,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
        })
        return "1. Reproduce the double-count with a test.\n2. Fix allow()."

    monkeypatch.setattr(p, "_run_rework_planner", _fake_rework_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9010)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcrework", "S1")

    assert result["ok"] is True
    assert len(rework_calls) == 1
    assert rework_calls[0]["review_feedback"] == "allow() double-counts refill on every call."

    append_content = popen_calls[0]["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "1. Reproduce the double-count with a test." in append_content
    # The original raw feedback stays present for reference, not replaced.
    assert "allow() double-counts refill on every call." in append_content


# Removed: test_dispatch_story_decompose_off_rework_uses_raw_feedback_unchanged
# asserted the now-removed "PIPELINE_DECOMPOSE=off skips the rework planner"
# behavior. Under always-on the rework planner runs for local-family dispatch;
# the raw-feedback fallback now lives only in the fail-open case, covered by
# test_dispatch_story_decompose_rework_fails_open_when_fix_planner_returns_none.


def test_dispatch_story_decompose_rework_fails_open_when_fix_planner_returns_none(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A failed rework-planner call must fall back to the raw-feedback
    format, never block or corrupt the rework redispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "dcreworknone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9012)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcreworknone", "S1")

    assert result["ok"] is True
    assert popen_calls[0]["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )


# Removed: test_dispatch_story_decompose_off_by_default_skips_planner asserted
# the now-removed "PIPELINE_DECOMPOSE unset = planner never invoked" behavior.
# Under always-on the planner runs for local-family dispatch; the positive
# case is covered by test_dispatch_story_planner_always_runs_for_local_when_
# decompose_unset in test_always_on_planner.py, and the claude-backend skip by
# test_dispatch_story_claude_backend_skips_planner there.


def test_dispatch_story_writes_plan_and_augments_local_prompt(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local-family dispatch (always-on planner) must run the planner, write
    the resulting checklist to .agent_plan.md, and augment the executor's
    task prompt with it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dccloud", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    planner_calls = []

    def _fake_planner(agent_instructions, *, dispatch_backend, local_model,
                      **kwargs):
        planner_calls.append({
            "agent_instructions": agent_instructions,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
        })
        return "1. Write a failing test for the limiter.\n2. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9002)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dccloud", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    assert planner_calls[0]["dispatch_backend"] == "local"
    assert planner_calls[0]["agent_instructions"] == "Build it."

    plan_path = worktree_root / "S1" / ".agent_plan.md"
    assert plan_path.exists()
    assert plan_path.read_text() == "1. Write a failing test for the limiter.\n2. Implement it."

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert "1. Write a failing test for the limiter." in task
    assert ".agent_scratchpad.md" in task


def test_dispatch_story_decompose_scratchpad_off_omits_scratchpad_instruction(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD=off is the H3 ablation arm (checklist
    only, no persistent scratchpad) - the checklist must still reach the
    executor, but the scratchpad instruction must not."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", "off")
    _write_manifest(plan_dir, "dcnoscratch", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner",
                        lambda *a, **k: "1. Write a failing test.\n2. Implement it.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9007)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcnoscratch", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert "1. Write a failing test." in task
    assert ".agent_scratchpad.md" not in task


def test_dispatch_story_decompose_scratchpad_defaults_on(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD unset must default to "on" - the
    scratchpad instruction ships by default whenever decompose is enabled."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcscratchdefault", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: "1. Step one.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9008)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcscratchdefault", "S1")

    assert result["ok"] is True
    assert ".agent_scratchpad.md" in popen_calls[0]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_decompose_passes_include_scratchpad_flag_to_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The scratchpad must be a first-class step IN the generated checklist,
    not just a trailing aside on the prompt. So when the scratchpad is enabled,
    dispatch must call _run_planner with include_scratchpad=True (so the
    planner weaves it into the steps); when disabled (H3 ablation), with
    include_scratchpad=False. Regression guard for the 9%-consumption root
    cause (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")

    def _run_dispatch(plan_name, story_key, scratchpad_env):
        if scratchpad_env is None:
            monkeypatch.delenv("PIPELINE_DECOMPOSE_SCRATCHPAD", raising=False)
        else:
            monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", scratchpad_env)
        # Distinct story_key per sub-dispatch: the worktree (and its
        # .agent_plan.md) is keyed by story_key, so reusing one would let the
        # second dispatch find the first's plan and skip _run_planner.
        _write_manifest(plan_dir, plan_name, {
            story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                        "status": "todo", "dependencies": []},
        })
        planner_kwargs = {}

        def _fake_planner(agent_instructions, **kwargs):
            planner_kwargs.update(kwargs)
            return "1. Step one."

        monkeypatch.setattr(p, "_run_planner", _fake_planner)
        monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
        monkeypatch.setattr(backend.subprocess, "Popen",
                            lambda cmd, env, **kw: _FakeProc(9009))
        monkeypatch.setattr(pt, "plane_request",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
        monkeypatch.setattr(p, "_default_branch", lambda: "main")
        assert p.dispatch_story(plan_name, story_key)["ok"] is True
        return planner_kwargs

    # Default (unset) -> on -> planner told to include the scratchpad step.
    assert _run_dispatch("dcincl_default", "SDEF", None)["include_scratchpad"] is True
    # Explicit on -> same.
    assert _run_dispatch("dcincl_on", "SON", "on")["include_scratchpad"] is True
    # H3 ablation off -> planner must NOT weave it in.
    assert _run_dispatch("dcincl_off", "SOFF", "off")["include_scratchpad"] is False


def test_dispatch_story_passes_plan_role_config_from_manifest_to_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """End-to-end: a plan's manifest role_config block must actually reach
    _run_planner's plan_role_config kwarg via dispatch_story - not just be
    tolerated by signature."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (plan_dir / "dpcfg.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "SPC": {"summary": "Do thing", "agent_instructions": "Build it.",
                     "status": "todo", "dependencies": []},
        },
        "repo_root": str(plan_dir),
        "role_config": {"planner": {"provider": "mlx"}},
    }))
    captured = {}

    def _fake_planner(agent_instructions, **kwargs):
        captured.update(kwargs)
        return "1. Step one."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env, **kw: _FakeProc(9009))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    assert p.dispatch_story("dpcfg", "SPC")["ok"] is True

    assert captured["plan_role_config"] == {"planner": {"provider": "mlx"}}


def test_dispatch_story_decompose_skips_for_claude_backend(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The checklist crutch exists for the weak local executor only - a
    Claude dispatch must never trigger planning even with decompose enabled."""
    _write_manifest(plan_dir, "dcclaude", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9003))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcclaude", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".agent_plan.md").exists()


def test_dispatch_story_claude_backend_gets_scratchpad_instruction(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The scratchpad-maintenance instruction is independent of the
    tech-lead checklist crutch: a Claude dispatch (the default when
    PIPELINE_BACKEND_DISPATCH is unset, and with PIPELINE_DECOMPOSE_SCRATCHPAD
    also left at its "on" default) must still be told to keep
    .agent_scratchpad.md up to date, even though it never gets the
    checklist-from-your-tech-lead framing (that phase stays local-only)."""
    _write_manifest(plan_dir, "clscratchon", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, **kw):
        popen_calls.append({"cmd": cmd})
        return _FakeProc(9101)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("clscratchon", "S1")

    assert result["ok"] is True
    prompt = popen_calls[0]["cmd"][2]
    assert ".agent_scratchpad.md" in prompt
    assert "Implementation checklist from your tech lead" not in prompt


def test_dispatch_story_claude_backend_scratchpad_off_omits_instruction(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """H3-ablation parity for Claude: PIPELINE_DECOMPOSE_SCRATCHPAD=off must
    suppress the scratchpad instruction for a Claude dispatch exactly as it
    does for local-family dispatch."""
    monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", "off")
    _write_manifest(plan_dir, "clscratchoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, **kw):
        popen_calls.append({"cmd": cmd})
        return _FakeProc(9102)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("clscratchoff", "S1")

    assert result["ok"] is True
    prompt = popen_calls[0]["cmd"][2]
    assert ".agent_scratchpad.md" not in prompt


def test_dispatch_story_local_backend_checklist_unaffected_by_claude_change(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard: the new Claude-only scratchpad branch must be
    mutually exclusive with the existing `if checklist_is_fresh:` branch -
    local-family dispatch keeps its checklist framing AND the
    PROGRESS: <done>/<total> checklist-total format, unchanged."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "localcheckunaffected", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: "1. Step one.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9103)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("localcheckunaffected", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert ".agent_scratchpad.md" in task
    assert "Implementation checklist from your tech lead" in task
    assert "PROGRESS: <done>/<total>" in task


def test_dispatch_story_decompose_fails_open_when_planner_returns_none(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A planner that fails (returns None) must not block or alter dispatch -
    the story proceeds exactly like PIPELINE_DECOMPOSE=off."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcnone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9004)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcnone", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".agent_plan.md").exists()
    # test_author now resolves to claude/sonnet (model_registry.json), so it
    # issues its own leading Popen call (claude backend env, no
    # LOCAL_AGENT_TASK) before the local executor's - assert against the
    # last call, which is the executor's.
    assert ".agent_scratchpad.md" not in popen_calls[-1]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_decompose_skips_replanning_on_resume_but_keeps_referencing_plan(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A resumed (interrupted) local story whose worktree already carries a
    plan from its first dispatch must NOT call the planner again (spending a
    second LLM call) but the rebuilt resume prompt must still reference the
    existing checklist, so the executor doesn't lose the guidance on resume."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_plan.md").write_text("1. Existing checklist step.")
    # The checklist-reuse guard (server.py:~1899) requires a .agent_plan_src_hash
    # whose content matches a sha256 of the story's agent_instructions, else a
    # resumed dispatch silently drops the checklist from the rebuilt prompt.
    (worktree_path / ".agent_plan_src_hash").write_text(
        hashlib.sha256(b"Build it.").hexdigest()
    )
    _write_manifest(plan_dir, "dcresume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "worktree": str(worktree_path),
               "last_commit": "sha-1"},
    })
    (plan_dir / "dcresume.S1.journal.json").write_text(json.dumps([
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]))

    def _boom(*a, **k):
        raise AssertionError("planner must not re-run on a resumed dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9005)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcresume", "S1")

    assert result["ok"] is True
    assert result["resumed"] is True
    # Plan file untouched - not overwritten by a (forbidden) re-plan call.
    assert (worktree_path / ".agent_plan.md").read_text() == "1. Existing checklist step."
    assert "1. Existing checklist step." in popen_calls[0]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_excludes_decompose_artifacts_from_worktree_tracking(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Mirrors test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking
    (Mode 17) for the two new guided-decomposition artifacts: an untracked
    .agent_plan.md/.agent_scratchpad.md that later gets swept into a rework
    WIP-commit's `git add -A` would dirty the tree ahead of the pre-merge
    rebase the same way review.log did."""
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=real_repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                    cwd=real_repo, check=True)
    bare_origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare_origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_origin)], cwd=real_repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=real_repo, check=True)

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcexcl", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "dcexcl.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(p, "_run_planner",
                        lambda *a, **k: "1. Checklist step.")
    real_popen = backend.subprocess.Popen
    real_popen_calls = []

    def _discriminating_popen(cmd, **kw):
        if cmd and str(cmd[0]).endswith("python3"):
            return _FakeProc(9006)
        if cmd and str(cmd[0]).endswith("claude"):
            # The test-author phase spawns a real `claude -p`; fake it like
            # the python3 executor spawn so this unit test never launches
            # the real binary. The phase then falls open on no-commits and
            # the monolithic dispatch path proceeds unchanged.
            return _FakeProc(9007)
        real_popen_calls.append(cmd)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    result = p.dispatch_story("dcexcl", "S1")
    assert result["ok"] is True

    exclude_content = (real_repo / ".git" / "info" / "exclude").read_text()
    assert ".agent_plan.md" in exclude_content
    assert ".agent_scratchpad.md" in exclude_content

    worktree_path = worktree_root / "S1"
    assert (worktree_path / ".agent_plan.md").exists()
    (worktree_path / ".agent_scratchpad.md").write_text("step 1 done\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=worktree_path, capture_output=True, text=True, check=True)
    assert ".agent_plan.md" not in staged.stdout
    assert ".agent_scratchpad.md" not in staged.stdout
    # subprocess.run() is implemented via Popen internally, so the legitimate
    # git fetch/worktree/add calls this flow makes through subprocess.run
    # also land here - only assert none of them is a non-git (e.g. a leaked
    # real `claude`) spawn.
    assert all(cmd and cmd[0] == "git" for cmd in real_popen_calls)


# ---------- dispatch_story wiring for the TDD-split test-author phase
# (TDD_SPLIT_PRODUCTION_PLAN.md §2.1/§2.4) ----------
def test_dispatch_story_tdd_split_unset_opted_in_runs_test_author_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With the global PIPELINE_TDD_SPLIT toggle removed, an opted-in
    (tdd_split=true) non-resuming story RUNS the test-author phase even
    with the env var unset - the per-story tdd_split field is the sole
    opt-in (Secure Defaults: opt-in is per story, not per env)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })

    def _run(*a, **k):
        nonlocal ran
        ran = True
        return True

    ran = False
    monkeypatch.setattr(p, "_run_test_author_phase", _run)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9101))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdoff", "S1")

    assert result["ok"] is True
    assert ran is True
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()

def test_dispatch_story_tdd_split_opted_in_runs_phase_and_augments_prompt(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An opted-in (tdd_split=true) non-resuming story runs the test-author
    phase and the executor prompt is augmented with the NEVER-touch-tests
    steering (the global PIPELINE_TDD_SPLIT toggle is gone; opt-in is solely
    the per-story field)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdon", {
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
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9103)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdon", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1
    assert phase_calls[0]["story_key"] == "S1"
    assert phase_calls[0]["worktree_path"] == worktree_root / "S1"
    assert phase_calls[0]["dispatch_backend"] == "local"

    marker = worktree_root / "S1" / ".tdd_split_test_author_done"
    assert marker.exists()

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


def test_dispatch_story_tdd_split_phase_failure_falls_open_to_monolithic(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A test-author phase that fails (returns False - unresolved role,
    dispatch error, timeout, or no commit) must leave no marker and must
    NOT augment the executor prompt - the story proceeds exactly like a
    story with no split configured (§2.5's fail-open contract)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdfail", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })

    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9104)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdfail", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task


def test_dispatch_story_tdd_split_skips_rerun_when_marker_already_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch on a worktree that already has a test-author
    commit must not run the phase again (§2.1: reworks act on the SAME
    tests) - but the executor prompt must still reference them."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdmarker", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "dependencies": [], "tdd_split": True},
    })
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".tdd_split_test_author_done").write_text("ok\n")

    def _boom(*a, **k):
        raise AssertionError("must not re-run the test-author phase on a resumed worktree")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9105)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdmarker", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


def test_dispatch_story_tdd_split_story_without_opt_in_field_still_runs_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The per-story `tdd_split` opt-in field has been removed as a gate
    (see PLAN_RETROSPECTIVE_PROCESS_PLAN.md / retros/tdd-split-always-on):
    the split is now unconditional for local-family dispatch, mirroring the
    guided-decomposition planner. A story with no `tdd_split` key at all
    still runs the test-author phase - there is no per-story escape hatch
    anymore, matching the operator's directive that removing the toggle
    meant "always on for every story", not "opt-in per story"."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdnoopt", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    ran = False

    def _run(*a, **k):
        nonlocal ran
        ran = True
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _run)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9102))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdnoopt", "S1")

    assert result["ok"] is True
    assert ran is True
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
