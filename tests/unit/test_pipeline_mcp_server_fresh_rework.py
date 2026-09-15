"""Tests for the pipeline MCP server: fresh-rework-on-regression.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import subprocess

from app import (
    backend,
    backend_ollama,
)
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

# ---------- Cloud-aware per-story dispatch gate ----------
# A :cloud-tagged model (e.g. deepseek-v4-flash:cloud) is served via Ollama
# with zero local VRAM footprint, so the local free-memory floor must not
# gate it. On-device models keep the floor exactly as before. These drive
# advance_pipeline through the real path (not the helper in isolation).

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise backend.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )

    def json(self):
        return self._payload

def _cloud_gate_manifest():
    return {
        "C1": {"summary": "cloud story", "agent_instructions": "Do it.",
               "status": "todo", "dependencies": [],
               "model": "deepseek-v4-flash:cloud", "backend": "ollama"},
        "D1": {"summary": "on-device story", "agent_instructions": "Do it.",
               "status": "todo", "dependencies": [],
               "model": "gemma4:26b-a4b-it-qat", "backend": "ollama"},
    }


def test_advance_pipeline_dispatches_cloud_story_under_memory_pressure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A :cloud story must dispatch even when local free memory is below the
    floor, while a sibling on-device story is deferred (floor still applies)."""
    _write_manifest(plan_dir, "cloudgate", _cloud_gate_manifest())
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})
    # Local free memory below the 2048mb floor.
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    # Reachability must be ok (the gate checks it first, even for :cloud).
    monkeypatch.setattr(backend.httpx, "get", lambda url, timeout: _FakeResponse({}))
    monkeypatch.setattr(backend_ollama, "_total_memory_mb", lambda: 24576)

    result = p.advance_pipeline("cloudgate")

    assert result["ok"] is True
    assert "C1" in result["dispatched"], f"cloud story must dispatch: {result}"
    assert "D1" not in result["dispatched"], "on-device story must be deferred"
    stories = _read_manifest(plan_dir, "cloudgate")["stories"]
    assert stories["C1"]["status"] == "in_progress"
    assert stories["D1"]["status"] == "todo"


def test_advance_pipeline_does_not_interrupt_cloud_in_progress_on_memory_pressure(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """A :cloud in-progress story must NOT be interrupted by the local memory
    gate, while an on-device in-progress story is NOT interrupted when
    its gate failure reason is memory pressure."""
    _write_manifest(plan_dir, "cloudint", {
        "A1": {"summary": "cloud running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wta"), "dependencies": [],
               "model": "deepseek-v4-flash:cloud", "backend": "ollama"},
        "B1": {"summary": "on-device running", "status": "in_progress", "pid": 222,
               "worktree": str(tmp_path / "wtb"), "dependencies": [],
               "model": "gemma4:26b-a4b-it-qat", "backend": "ollama"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(backend.httpx, "get", lambda url, timeout: _FakeResponse({}))

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("cloudint")

    assert result["ok"] is True
    assert "A1" not in result["interrupted"], "cloud in-progress must not be interrupted"
    assert "B1" not in result["interrupted"], "on-device in-progress must NOT be interrupted on memory pressure"
    stories = _read_manifest(plan_dir, "cloudint")["stories"]
    assert stories["A1"]["status"] == "in_progress"
    assert stories["B1"]["status"] == "in_progress"


def test_advance_pipeline_polls_cloud_in_progress_under_downed_blanket_gate(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """A :cloud in-progress story must be POLLED even when the blanket dispatch
    gate is down for memory pressure. Dispatch and interruption are per-story, so
    a :cloud story can be dispatched (and left running) while the blanket local
    gate is down; if polling were still blanket-gated it would stall in_progress
    forever. A sibling on-device story whose own floor is not met is NOT interrupted
    (its gate failure is memory pressure) but is still skipped by the poll gate."""
    _write_manifest(plan_dir, "cloudpoll", {
        "A1": {"summary": "cloud running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wta"), "dependencies": [],
               "model": "deepseek-v4-flash:cloud", "backend": "ollama"},
        "B1": {"summary": "on-device running", "status": "in_progress", "pid": 222,
               "worktree": str(tmp_path / "wtb"), "dependencies": [],
               "model": "gemma4:26b-a4b-it-qat", "backend": "ollama"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    # Blanket dispatch gate DOWN for memory pressure: the exact condition under
    # which a cloud story can be dispatched but, pre-fix, was never polled.
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (False, "insufficient free memory for local dispatch"),
    )
    polled = []
    monkeypatch.setattr(
        p, "check_story_status",
        lambda plan, key: polled.append(key) or {"status": "running"},
    )
    # B1's own floor not met -> NOT interrupted (memory pressure), but still
    # skipped by the poll gate.
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(backend.httpx, "get", lambda url, timeout: _FakeResponse({}))

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("cloudpoll")

    assert result["ok"] is True
    assert "A1" in polled, f"cloud in-progress must be polled with blanket gate down: {result}"
    assert "B1" not in polled, "on-device in-progress whose floor is not met must not be polled"
    assert "A1" not in result["interrupted"], "cloud in-progress must not be interrupted"
    assert "B1" not in result["interrupted"], "on-device in-progress whose floor is not met must NOT be interrupted on memory pressure"


def test_advance_pipeline_still_gates_cloud_story_when_unreachable(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A :cloud story is NOT exempt from reachability - an unreachable server
    gates it regardless of the :cloud tag."""
    _write_manifest(plan_dir, "cloudunreach", {
        "C1": {"summary": "cloud story", "agent_instructions": "Do it.",
               "status": "todo", "dependencies": [],
               "model": "deepseek-v4-flash:cloud", "backend": "ollama"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    # Make the ollama backend unreachable.
    def _boom(url, timeout):
        raise backend.httpx.ConnectError("connection refused")
    monkeypatch.setattr(backend.httpx, "get", _boom)

    result = p.advance_pipeline("cloudunreach")

    assert result["ok"] is True
    assert "C1" not in result["dispatched"], "unreachable cloud story must not dispatch"
    stories = _read_manifest(plan_dir, "cloudunreach")["stories"]
    assert stories["C1"]["status"] == "todo"


def test_claude_routed_story_gated_by_usage_not_memory(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story routing to 'claude' is gated by Claude's usage resource_status,
    NOT by the local memory floor."""
    _write_manifest(plan_dir, "claudegate", {
        "S1": {"summary": "security story", "agent_instructions": "Do it.",
               "status": "todo", "dependencies": [],
               "risk": "high"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(backend.httpx, "get", lambda url, timeout: _FakeResponse({}))

    # Sub-case 1: Claude usage gated -> deferred.
    monkeypatch.setattr(
        backend.ClaudeCliDriver, "resource_status",
        lambda self, model_tag=None: {"ok": False, "reason": "usage exhausted"},
    )
    result = p.advance_pipeline("claudegate")
    assert "S1" not in result["dispatched"], "claude-gated story must be deferred"
    assert _read_manifest(plan_dir, "claudegate")["stories"]["S1"]["status"] == "todo"

    # Sub-case 2: Claude usage ok -> dispatched.
    monkeypatch.setattr(
        backend.ClaudeCliDriver, "resource_status",
        lambda self, model_tag=None: {"ok": True, "reason": ""},
    )
    result = p.advance_pipeline("claudegate")
    assert "S1" in result["dispatched"], "claude-ok story must dispatch"
    assert _read_manifest(plan_dir, "claudegate")["stories"]["S1"]["status"] == "in_progress"


def test_default_model_story_gated_under_memory_pressure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story with model=None resolves to the env-default on-device model, so
    the local memory floor applies - it must be gated under memory pressure
    (matches today's behavior)."""
    _write_manifest(plan_dir, "defaultgate", {
        "S1": {"summary": "default story", "agent_instructions": "Do it.",
               "status": "todo", "dependencies": [],
               "backend": "ollama"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})
    monkeypatch.setattr(backend.OllamaDriver, "_free_memory_mb", lambda self: 500)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gemma4:26b-a4b-it-qat")
    monkeypatch.setattr(backend.httpx, "get", lambda url, timeout: _FakeResponse({}))

    result = p.advance_pipeline("defaultgate")

    assert result["ok"] is True
    assert "S1" not in result["dispatched"], "default on-device story must be gated"
    assert _read_manifest(plan_dir, "defaultgate")["stories"]["S1"]["status"] == "todo"


def test_dispatch_story_proceeds_when_lock_free(plan_dir, worktree_root, agents_dir, monkeypatch):
    """Sanity check: with no lock held, dispatch_story runs normally. Catches
    a regression where the lock is held unconditionally (no caller would ever
    proceed) or released too early (a second call races in mid-dispatch)."""
    _write_manifest(plan_dir, "dlk2", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1357))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dlk2", "S1")

    assert result["ok"] is True
    assert result["pid"] == 1357
    assert result.get("skipped") is None


# ---------- Resume auto-rebase onto master (Mode 53 follow-up) ----------
# On resume, when the worktree base is behind origin/<default>, dispatch must
# actually rebase the worktree onto origin/<default> BEFORE the agent starts
# (replacing the old notify-only observability hook). A rebase conflict parks
# the story fail-secure; any other rebase failure fails open (proceed).


def _resume_rebase_harness(monkeypatch, plan_dir, worktree_root, behind,
                           rebase_result, plan_name="rrb", story_key="S1"):
    """Set up a resumed dispatch whose worktree base is `behind` commits behind
    origin/<default>. Mocks at the true external boundary: subprocess.run (to
    control the `git rev-list --count` behind-count) and `_rebase_onto_master`
    (to return canned dicts). Returns (worktree_path, notes, rebase_calls)."""
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": "interrupted", "dependencies": []},
    })
    wt = worktree_root / story_key
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /fake\n")

    notes = []
    rebase_calls = []

    def _fake_run(cmd, cwd=None, **kwargs):
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, str(behind), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        p, "_rebase_onto_master",
        lambda wt_arg, br: rebase_calls.append((wt_arg, br)) or rebase_result,
    )
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(777))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: notes.append(msg))
    return wt, notes, rebase_calls


def test_resume_and_rebase_behind_ok_proceeds(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """behind > 0 and rebase ok=True: _rebase_onto_master IS called with
    (worktree_path, branch), dispatch proceeds (agent started / IN_PROGRESS),
    and an info notification says the worktree was rebased onto origin/main."""
    wt, notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plan_dir, worktree_root, behind=3,
        rebase_result={"ok": True, "conflict": False, "error": "",
                       "auto_resolved": False},
    )
    result = p.dispatch_story("rrb", "S1")

    assert result["ok"] is True
    assert result["pid"] == 777
    assert rebase_calls == [(wt, "agent/s1")]
    assert any("rebased onto origin/main" in m for m in notes), notes
    story = _read_manifest(plan_dir, "rrb")["stories"]["S1"]
    assert story["status"] == "in_progress"


def test_resume_and_rebase_behind_ok_auto_resolved_proceeds(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """behind > 0 and rebase ok=True with auto_resolved=True: additive-import
    auto-resolution is safe, so dispatch proceeds."""
    wt, _notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plan_dir, worktree_root, behind=3,
        rebase_result={"ok": True, "conflict": False, "error": "",
                       "auto_resolved": True},
    )
    result = p.dispatch_story("rrb", "S1")

    assert result["ok"] is True
    assert result["pid"] == 777
    assert rebase_calls == [(wt, "agent/s1")]
    story = _read_manifest(plan_dir, "rrb")["stories"]["S1"]
    assert story["status"] == "in_progress"


def test_resume_and_rebase_behind_zero_no_rebase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """behind == 0: _rebase_onto_master is NOT called (no unnecessary rebase);
    dispatch proceeds as today."""
    _wt, notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plan_dir, worktree_root, behind=0,
        rebase_result={"ok": True, "conflict": False, "error": "",
                       "auto_resolved": False},
    )
    result = p.dispatch_story("rrb", "S1")

    assert result["ok"] is True
    assert result["pid"] == 777
    assert rebase_calls == []
    assert not any("rebased onto origin/main" in m for m in notes), notes


def test_resume_and_rebase_conflict_parks(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """behind > 0 and rebase ok=False, conflict=True: the story is PARKED with
    a clear parked_reason naming the rebase conflict and that the worktree is
    still behind origin/main; a notification is emitted; the agent is NOT
    dispatched on the stale base (fail-secure)."""
    wt, notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plan_dir, worktree_root, behind=3,
        rebase_result={"ok": False, "conflict": True,
                       "error": "non-additive conflict in src/app.py",
                       "auto_resolved": False},
    )
    result = p.dispatch_story("rrb", "S1")

    story = _read_manifest(plan_dir, "rrb")["stories"]["S1"]
    assert story["status"] == "parked"
    assert "rebase conflict" in story["parked_reason"]
    assert "still behind origin/main" in story["parked_reason"]
    assert rebase_calls == [(wt, "agent/s1")]
    assert any("parked" in m.lower() for m in notes), notes
    assert "pid" not in result  # agent NOT dispatched on the stale base


def test_resume_and_rebase_other_failure_fail_open(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """behind > 0 and rebase ok=False, conflict=False (other git/infra
    failure): fail open - notify + proceed with dispatch (today's behavior)."""
    wt, _notes, rebase_calls = _resume_rebase_harness(
        monkeypatch, plan_dir, worktree_root, behind=2,
        rebase_result={"ok": False, "conflict": False,
                       "error": "git fetch timeout", "auto_resolved": False},
    )
    result = p.dispatch_story("rrb", "S1")

    assert result["ok"] is True
    assert result["pid"] == 777
    assert rebase_calls == [(wt, "agent/s1")]
    story = _read_manifest(plan_dir, "rrb")["stories"]["S1"]
    assert story["status"] == "in_progress"


def test_resume_and_rebase_no_git_skipped(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Resumed path with no `.git` at the worktree root: the staleness check is
    skipped entirely (today's `.git` existence guard preserved) - no rebase, no
    notification, dispatch proceeds."""
    _write_manifest(plan_dir, "rrb", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "dependencies": []},
    })
    wt = worktree_root / "S1"
    wt.mkdir(parents=True, exist_ok=True)  # no .git file/dir at root

    notes = []
    rebase_calls = []
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(
        p, "_rebase_onto_master",
        lambda wt_arg, br: rebase_calls.append((wt_arg, br))
        or {"ok": True, "conflict": False, "error": "", "auto_resolved": False},
    )
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(999))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: notes.append(msg))

    result = p.dispatch_story("rrb", "S1")

    assert result["ok"] is True
    assert result["pid"] == 999
    assert rebase_calls == []
    assert not any("rebased onto origin/main" in m for m in notes), notes


