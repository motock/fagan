"""Tests for the pipeline MCP server: _reverify_build.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
from pathlib import Path

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)

# ---------- Local-first routing ----------

def test_route_dispatch_backend_low_risk_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "low", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_medium_risk_above_low_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_medium_risk_within_medium_ceiling_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_high_risk_above_medium_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_high_ceiling_allows_high_risk_local(monkeypatch):
    """Explicitly setting the ceiling to 'high' lets even high-risk go local."""
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_security_persona_always_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "low", "persona": "security-engineer"}) == "claude"


def test_route_dispatch_backend_missing_risk_defaults_low(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"persona": "software-engineer"}) == "local"


def test_dispatch_story_auto_routes_low_risk_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a low-risk story to OllamaDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "low", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(11))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("auto1", "S1")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "auto1")
    assert manifest["stories"]["S1"]["backend"] == "local"
    # OllamaDriver uses venv python, not "claude". test_author (claude/sonnet
    # per model_registry.json) issues its own leading Popen call first, so
    # the executor's call is the last one.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_auto_routes_high_risk_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a high-risk story to ClaudeCliDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "high", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(22))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("auto2", "S1")

    manifest = _read_manifest(plan_dir, "auto2")
    assert manifest["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_persists_backend_to_manifest(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The resolved backend is written to story['backend'] so escalation sees it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(33))
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb1", "S1")

    assert _read_manifest(plan_dir, "pb1")["stories"]["S1"]["backend"] == "local"


def test_dispatch_story_honors_stored_backend_override(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """story['backend'] takes precedence over env, used by escalation to lock to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "backend": "claude"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(44))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb2", "S1")

    # Even though env says local, the stored override wins → ClaudeCliDriver
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_security_persona_routes_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=local must not bypass the security-persona
    safety override: a security-engineer story still dispatches to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "security-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(55))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec1", "S1")

    assert _read_manifest(plan_dir, "sec1")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_non_security_persona_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Non-skip personas are unaffected by the security-persona override under
    explicit local dispatch - no regression from the new persona check."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "sec2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "software-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(56))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec2", "S1")

    assert _read_manifest(plan_dir, "sec2")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_local_missing_persona_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story with no persona field at all must not crash the override check
    and must fall through to the existing local resolution."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "sec3", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(57))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec3", "S1")

    assert _read_manifest(plan_dir, "sec3")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_local_security_persona_case_insensitive(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The persona override must use the same case-insensitive normalization
    as _route_dispatch_backend, so 'Security-Engineer' is still caught."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec4", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "Security-Engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(58))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec4", "S1")

    assert _read_manifest(plan_dir, "sec4")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_stored_backend_wins_over_security_persona(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An already-escalated story['backend'] (e.g. from _escalate_to_local_fallback_model)
    must not be re-routed by the persona check even if persona is security-engineer."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec5", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "persona": "security-engineer", "backend": "local"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(59))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec5", "S1")

    # story["backend"] was already explicitly "local" - the persona override
    # must not clobber it, even though persona is in _LOCAL_SKIP_PERSONAS.
    assert _read_manifest(plan_dir, "sec5")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_claude_security_persona_stays_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=claude is already always Claude; the persona
    override must be a no-op here (no regression)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec6", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "security-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(60))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec6", "S1")

    assert _read_manifest(plan_dir, "sec6")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


# ---------- Unwinnable-as-scoped safety override (Mode 40 retro #4) ----------
# A story whose agent_instructions describe a repo-wide, unscoped lint/fix
# sweep is structurally unwinnable for local dispatch: the done-bar demands
# every finding fixed, but a meaningful fraction of findings routinely land
# in test files (35/83 files on the live ruff baseline that motivated this),
# which the never-touch-tests steering forbids the local executor from
# editing. Detected and hard-routed to Claude, mirroring the existing
# security-persona override exactly.

def test_story_has_unwinnable_local_scope_detects_repo_wide_ruff_sweep():
    story = {"agent_instructions": "Run `.venv/bin/ruff check . --fix` from "
             "the repo root and fix every remaining finding."}
    assert p._story_has_unwinnable_local_scope(story) is True


def test_story_has_unwinnable_local_scope_detects_repo_wide_language():
    story = {"agent_instructions": "Fix every lint finding repo-wide."}
    assert p._story_has_unwinnable_local_scope(story) is True


def test_story_has_unwinnable_local_scope_false_for_scoped_lint_instructions():
    story = {"agent_instructions": "Run `ruff check pipeline/foo.py` and fix "
             "the two findings in that file."}
    assert p._story_has_unwinnable_local_scope(story) is False


def test_story_has_unwinnable_local_scope_false_for_missing_instructions():
    assert p._story_has_unwinnable_local_scope({}) is False


def test_story_has_unwinnable_local_scope_false_for_descriptive_ci_boilerplate_mention():
    """A bare, descriptive mention of 'repo-wide' inside a CI-reminder
    sentence is not an instruction to perform a repo-wide sweep and must
    not trip the override for an otherwise narrowly-scoped story."""
    story = {"agent_instructions": (
        "Add pipeline/foo.py implementing X. Do not modify any existing "
        "test file; add new ones only.\n\nCI runs `ruff check .` "
        "repo-wide and will reject the merge on any violation."
    )}
    assert p._story_has_unwinnable_local_scope(story) is False


def test_story_has_unwinnable_local_scope_still_detects_imperative_repo_wide_sweep():
    """Guard against a regex narrowed too far: differently-worded
    imperative repo-wide sweep instructions must still trip True."""
    story = {"agent_instructions":
             "Clean up every remaining lint error repo-wide before merging."}
    assert p._story_has_unwinnable_local_scope(story) is True


def test_route_dispatch_backend_unwinnable_scope_overrides_low_risk(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    story = {"risk": "low", "persona": "software-engineer",
              "agent_instructions": "Run `ruff check .` repo-wide and fix "
              "every finding."}
    assert p._route_dispatch_backend(story) == "claude"


def test_dispatch_story_explicit_local_unwinnable_scope_routes_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=local must not bypass the unwinnable-scope
    safety override: a repo-wide lint-sweep story still dispatches to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "scope1", {
        "S1": {"summary": "Adopt new lint ruleset",
               "agent_instructions": "Run `ruff check . --fix` from the repo "
               "root and manually fix every remaining finding.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(60))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("scope1", "S1")

    assert _read_manifest(plan_dir, "scope1")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_scoped_lint_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A lint-flavored story scoped to specific files (not a repo-wide sweep)
    is unaffected by the new override - no regression on ordinary stories."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "scope2", {
        "S1": {"summary": "Fix two lint findings",
               "agent_instructions": "Run `ruff check pipeline/foo.py` and "
               "fix the reported findings in that file only.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(61))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("scope2", "S1")

    assert _read_manifest(plan_dir, "scope2")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_advance_pipeline_escalates_local_failure_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local agent failure triggers clean Claude escalation (not terminal failure)
    under auto dispatch, where a-posteriori escalation is the intended fallback."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9001,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        # The internal `ps -p <pid> -o stat=` lookup should return empty so
        # check_story_status doesn't mistakenly treat the dead process as
        # "running". The test command itself returns _FailResult.
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc1")

    manifest = _read_manifest(plan_dir, "esc1")
    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert "pid" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "esc1.notifications.log").read_text()
    assert "escalating to Claude" in notif


def test_advance_pipeline_does_not_escalate_claude_failure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A Claude-run failure is terminal (not escalated again)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9002,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "claude", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    # Pretend the agent has exited: os.kill throws AND ps reports no such
    # process (empty stdout). check_story_status falls through both checks
    # and runs the test command (also mocked to fail). See esc1 for full
    # context.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc2")

    manifest = _read_manifest(plan_dir, "esc2")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_does_not_escalate_already_escalated(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story that already has escalated=True is not escalated a second time."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc3", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9003,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "escalated": True, "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    # The story's OWN pid (9003) must look dead (kill raises, ps returns
    # empty) for check_story_status to grade it. Other (zombie) pids also
    # raise; the production code's reap path turns this story's dead pid
    # back to "todo" — which is correct semantics for a real crashed agent.
    # See esc1 comment for the broader picture.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc3")

    manifest = _read_manifest(plan_dir, "esc3")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_escalates_local_failure_under_explicit_local_dispatch_with_auto_escalate_flag(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local agent failure escalates to Claude even when the dispatcher is
    explicitly pinned to ``local`` (PIPELINE_BACKEND_DISPATCH=local), as long
    as PIPELINE_AUTO_ESCALATE=1 turns the a-posteriori escalation gate on.

    This is the third escalation call site in advance_pipeline (the
    a-posteriori local-failure escalation). Before the fix it used a raw
    ``dispatch_mode == "auto"`` comparison, so under an explicit local backend
    escalation never fired and the story terminal-failed instead. After the fix
    it consults ``_auto_escalation_enabled()`` and honors the explicit opt-in.
    """
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc4", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9004,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc4")

    manifest = _read_manifest(plan_dir, "esc4")
    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert "pid" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "esc4.notifications.log").read_text()
    assert "escalating to Claude" in notif


def test_advance_pipeline_does_not_escalate_local_failure_when_auto_escalate_explicitly_off(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When PIPELINE_AUTO_ESCALATE=0 (explicit opt-out), a failing local story
    under auto dispatch does NOT escalate to Claude - it terminal-fails.

    This confirms the explicit opt-out (already proven at the escalation.py
    unit level in tests/unit/test_acceptance_autonomy_escalate_flag.py) is also
    honored at this specific a-posteriori call site in advance_pipeline, which
    was never covered here before. Before the fix this call site ignored
    PIPELINE_AUTO_ESCALATE entirely (it only checked dispatch_mode=="auto"),
    so the opt-out would have been silently bypassed and escalation would have
    fired anyway.
    """
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc5", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9005,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "0")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc5")

    manifest = _read_manifest(plan_dir, "esc5")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])
    assert story.get("backend") != "claude"


def test_advance_pipeline_aposteriori_escalation_uses_auto_escalation_gate():
    """The a-posteriori local-failure escalation call site in advance_pipeline
    must use the shared ``_auto_escalation_enabled()`` gate (not a raw
    ``dispatch_mode == "auto"`` env-var comparison), and the now-dead
    ``dispatch_mode`` local variable must be removed entirely from the file.

    These are mechanically-checkable requirements of the fix: the third call
    site is made consistent with the other two, and per project style the dead
    ``dispatch_mode`` definition is removed (not left unused/commented out).
    """
    import pipeline.server as srv

    source = Path(srv.__file__).read_text()

    # The a-posteriori escalation block's comment must reference the new gate,
    # not the old "auto dispatch only" wording.
    assert "_auto_escalation_enabled()" in source
    assert "A-posteriori escalation of a failed local run to Claude is gated by" in source
    # The old comment wording must be gone.
    assert (
        "A-posteriori escalation of a failed local run to Claude is a feature of"
        not in source
    )

    # The dead `dispatch_mode` local variable must be entirely gone - both its
    # definition and its single use at the escalation condition.
    assert "dispatch_mode" not in source


def test_advance_pipeline_give_up_failure_gets_distinguishing_notify(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story whose agent explicitly gave up (T6) gets a notify message
    pointing the human at "likely under-specified, needs clarification"
    rather than the generic "tests failed" - so a second identical
    dispatch/escalation attempt isn't the reflexive next step (2026-07-07
    web-client-epic retro §3.2: a missing API is a story-scoping bug, not a
    model-capability gap). Mirrors test_advance_pipeline_does_not_escalate_
    already_escalated's terminal-branch setup exactly, differing only in
    agent.log's content and the assertion on the notify message."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text(
        "[step 1] bash: grep -r PreKeyBundle .\n"
        "[step 9] DONE: I'm sorry, I can't complete this task.\n"
    )
    _write_manifest(plan_dir, "giveupnotify", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9003,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "escalated": True, "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.advance_pipeline("giveupnotify")

    manifest = _read_manifest(plan_dir, "giveupnotify")
    assert manifest["stories"]["S1"]["status"] == "failed"
    assert "S1" in result.get("failed", [])
    give_up_notes = [n for n in notes if "S1" in n and "gave up" in n]
    assert give_up_notes, notes
    assert "clarification" in give_up_notes[0]


def test_advance_pipeline_local_failure_terminal_under_local_mode(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Under explicit PIPELINE_BACKEND_DISPATCH=local, a failed local agent is
    terminal — it must NOT escalate to Claude (escalation is an auto-mode
    fallback). This keeps a local-only run from silently spending Claude."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esclocal", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9004,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    # See esc1 comment: agent is dead (kill raises, ps empty), test command
    # fails. The new reap path will turn this story's dead pid back to "todo"
    # first — that's why this test asserts the local-failure terminal path,
    # not the reap.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esclocal")

    story = _read_manifest(plan_dir, "esclocal")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["backend"] == "local"        # not flipped to claude
    assert not story.get("escalated")          # never escalated
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_retries_on_local_fallback_model(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A plan opted into manifest["local_model_fallback"] retries a failed
    local story on that model instead of parking immediately - and never on
    Claude, even though this is a local-only (not "auto") run, mirroring
    esclocal's terminal guard but with a fallback model configured."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9005,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "dispatched_model": "gpt-oss:20b",
                   "dependencies": []},
        },
    }))

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("escfallback")

    story = _read_manifest(plan_dir, "escfallback")["stories"]["S1"]
    assert story["status"] == "todo"
    assert story["backend"] == "local"                 # never claude
    assert story["model"] == "glm-5.2:cloud"
    assert story["tried_fallback_model"] is True
    assert "pid" not in story
    assert "dispatched_model" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "escfallback.notifications.log").read_text()
    assert "retrying on fallback model glm-5.2:cloud" in notif


def test_advance_pipeline_fallback_model_failure_is_terminal(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Once the fallback model has already been tried (tried_fallback_model),
    a second failure is terminal - it must not retry forever, and must not
    escalate to Claude either."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback2.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9006,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "model": "glm-5.2:cloud",
                   "tried_fallback_model": True, "dependencies": []},
        },
    }))

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("escfallback2")

    story = _read_manifest(plan_dir, "escfallback2")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["backend"] == "local"         # never claude
    assert "S1" in result.get("failed", [])


