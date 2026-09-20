"""Tests for the "the scratchpad is gitignored by design - never commit it"
steering that must reach the executor through BOTH deterministic channels.

Live failure 2026-09-20 (story FSU-01): the planner-generated checklist's
last step told the agent the worktree diff should touch only the
implementation files "(plus the scratchpad file)". That phrasing was
planner-LLM prose, not repo source, so no template edit alone can pin it.
The agent resolved the contradiction against the gitignore with
`git add -f`, which tracked .agent_scratchpad.md, reddened 7
scratchpad-tracking tests, and cost a full rework cycle on an otherwise
correct change.

.agent_scratchpad.md is gitignored BY DESIGN; the worktree diff must never
contain it. Two deterministic channels reach the executor and both must
carry the rule:

  1. pipeline/planner.py's _PLANNER_SCRATCHPAD_CLAUSE - spliced into the
     planner's system prompt when the scratchpad is on, so the checklist the
     planner writes carries it.
  2. pipeline/dispatch.py's scratchpad_instruction backstop - the trailing
     reminder appended to the assembled checklist prompt, which the executor
     reads verbatim even when the planner under-emits the clause.

Assertions here are membership-only on the shared prompt strings (never the
clause's exact full text or length), so later stories can keep extending
them.
"""
from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _FakePlannerBackend,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)

# The two literals the rule must carry, in both channels.
_RULE_LITERALS = ("gitignored by design", "never commit it")


def _planner_system_prompt(monkeypatch, *, include_scratchpad):
    """Run the planner once and return the system prompt it was handed."""
    fake = _FakePlannerBackend(response="1. Step one.")
    # Scoped so a test that also drives a dispatch (which resolves its own
    # backend through backend.get_backend) is not left with the planner fake.
    with monkeypatch.context() as m:
        m.setattr(backend, "get_backend", lambda role, *, name=None: fake)

        p._run_planner(
            "Add a rate limiter.", dispatch_backend="local",
            local_model="gpt-oss:20b", include_scratchpad=include_scratchpad,
        )

    return fake.calls[0]["system"]


def _local_dispatch_prompt(monkeypatch, plan_dir_path, worktree_root_path, plan_name):
    """Dispatch a scratchpad-on local story and return the assembled prompt
    the executor is handed (the local driver passes it via LOCAL_AGENT_TASK;
    fall back to the positional prompt arg for robustness)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir_path, plan_name, {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # so popen_calls captures only the main executor's dispatch, not an
    # incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: "1. Step one.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9201)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    assert p.dispatch_story(plan_name, "S1")["ok"] is True

    call = popen_calls[0]
    env = call.get("env") or {}
    if "LOCAL_AGENT_TASK" in env:
        return env["LOCAL_AGENT_TASK"]
    return call["cmd"][2]


def _claude_dispatch_prompt(monkeypatch, plan_dir_path, worktree_root_path, plan_name):
    """Dispatch a scratchpad-on Claude story (PIPELINE_BACKEND_DISPATCH left
    unset -> claude default) and return the assembled prompt."""
    _write_manifest(plan_dir_path, plan_name, {
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
        return _FakeProc(9202)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    assert p.dispatch_story(plan_name, "S1")["ok"] is True

    return popen_calls[0]["cmd"][2]


# ---------- Channel 1: the planner's system prompt ----------


def test_should_tell_the_planner_the_scratchpad_is_never_committed(
    agents_dir, monkeypatch,  # noqa: F811
):
    """With include_scratchpad=True the planner's system prompt must carry the
    never-commit rule, so the checklist the planner writes tells the executor
    the scratchpad is gitignored by design and must never be committed."""
    system = _planner_system_prompt(monkeypatch, include_scratchpad=True)

    for literal in _RULE_LITERALS:
        assert literal in system
    # The live failure was resolved with `git add -f`; the rule must name it.
    assert "git add -f" in system
    # ... and it must state the diff invariant the agent violated.
    assert "The worktree diff must contain only" in system


# ---------- Channel 2: the executor's assembled dispatch prompt ----------


def test_should_tell_the_executor_the_scratchpad_is_never_committed(
    plan_dir, worktree_root, agents_dir, monkeypatch,  # noqa: F811
):
    """The trailing backstop appended to the assembled checklist prompt must
    carry the same rule: the executor reads it verbatim even when the planner
    under-emits the clause."""
    prompt = _local_dispatch_prompt(
        monkeypatch, plan_dir, worktree_root, "scrnevertell",
    )

    for literal in _RULE_LITERALS:
        assert literal in prompt
    assert "git add -f" in prompt
    # It belongs to the backstop that follows the checklist, not somewhere
    # else in the prompt.
    assert prompt.index("Work through these steps in order.") < prompt.index(
        "gitignored by design"
    )


# ---------- Survivor guard: the rule is additive, not a replacement ----------


def test_should_keep_the_scratchpad_instructions_when_the_rule_is_added(
    plan_dir, worktree_root, agents_dir, monkeypatch,  # noqa: F811
):
    """Adding the never-commit rule must not displace the existing scratchpad
    instructions: the clause still tells the planner to make creating
    .agent_scratchpad.md the FIRST step, and the backstop still names the
    PROGRESS: <done>/<total> first line."""
    system = _planner_system_prompt(monkeypatch, include_scratchpad=True)
    assert ".agent_scratchpad.md" in system
    assert "FIRST step" in system
    # ... and the per-step append instruction survives too.
    assert "appending its progress" in system

    prompt = _local_dispatch_prompt(
        monkeypatch, plan_dir, worktree_root, "scrkeepinstr",
    )
    assert ".agent_scratchpad.md" in prompt
    assert "After finishing each step" in prompt
    assert "PROGRESS:" in prompt
    assert "The FIRST line must be" in prompt


# ---------- Negative / boundary cases ----------


def test_should_not_carry_the_rule_when_the_scratchpad_is_off(
    agents_dir, monkeypatch,  # noqa: F811
):
    """The clause is opt-in: include_scratchpad=False must not carry the new
    sentence (the H3 ablation arm stays clean)."""
    system = _planner_system_prompt(monkeypatch, include_scratchpad=False)

    for literal in _RULE_LITERALS:
        assert literal not in system
    assert "git add -f" not in system


def test_should_not_carry_the_rule_when_scratchpad_gating_is_off(
    plan_dir, worktree_root, agents_dir, monkeypatch,  # noqa: F811
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD=off must suppress the rule along with the
    rest of the scratchpad steering - the gating is unchanged."""
    monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", "off")
    prompt = _local_dispatch_prompt(
        monkeypatch, plan_dir, worktree_root, "scrgateoff",
    )

    for literal in _RULE_LITERALS:
        assert literal not in prompt
    assert ".agent_scratchpad.md" not in prompt


def test_non_local_parity_branch_gains_no_progress_line(
    plan_dir, worktree_root, agents_dir, monkeypatch,  # noqa: F811
):
    """The non-local-family (Claude) parity branch deliberately has no
    numbered-step total to report progress against: it must keep its
    scratchpad instruction and must NOT gain a PROGRESS line."""
    prompt = _claude_dispatch_prompt(
        monkeypatch, plan_dir, worktree_root, "scrclaudeparity",
    )

    assert ".agent_scratchpad.md" in prompt
    assert "PROGRESS:" not in prompt
