"""Tests for the pipeline MCP server: advance_pipeline orchestration (part 1).

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

import pytest

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _STEP_CAP_MARKER_LOCAL,
    _STEP_CAP_MARKER_ORACLE,
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _make_fake_git_run,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)

# ---------- Escalation retarget (PIPELINE_ESCALATION_BACKEND/MODEL) ----------
# While Claude usage is capped, an operator can retarget escalation away from
# Claude so an escalated story re-dispatches/re-reviews on a non-Claude
# provider instead of failing against an unavailable Claude. The default
# (env unset) must preserve the original claude flip exactly, so the many
# existing escalation tests - none of which set these env vars - stay green.

def test_escalation_target_defaults_to_claude_with_no_model(monkeypatch):
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    assert p._escalation_target() == ("claude", None)


def test_escalation_target_env_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")
    assert p._escalation_target() == ("ollama", "deepseek-v4-flash:cloud")


def test_escalation_target_blank_model_yields_none(monkeypatch):
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "  ")
    assert p._escalation_target() == ("ollama", None)


def test_escalate_to_claude_retargets_to_env_backend_and_model(
    plan_dir, tmp_path, monkeypatch,
):
    """PIPELINE_ESCALATION_BACKEND/MODEL retarget the dispatch-failure
    escalation: the story flips to the configured backend (not Claude) and its
    model is set to the configured model so the next dispatch runs on it."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escesc.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "model": "gemma4:26b-a4b-it-qat",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")

    p._escalate_to_claude(manifest, "escesc", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert story["backend"] == "ollama"
    assert story["model"] == "deepseek-v4-flash:cloud"
    assert story["escalated"] is True
    assert story["status"] == "todo"


def test_escalate_to_claude_default_leaves_model_untouched(
    plan_dir, tmp_path, monkeypatch,
):
    """Default escalation target (claude, no model) must NOT overwrite or clear
    the story's existing model field - preserving the original behavior where
    Claude dispatch resolves its own model and the escalation only flips the
    backend."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escdef.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "model": "gemma4:26b-a4b-it-qat",
                   "dispatch_attempts": 1},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)

    p._escalate_to_claude(manifest, "escdef", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story.get("model") == "gemma4:26b-a4b-it-qat"


def test_escalate_review_to_claude_retargets_to_env_backend_and_model(monkeypatch):
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")
    story = {"backend": "local", "model": "gemma4:26b-a4b-it-qat",
             "rework_attempts": 3, "review_inconclusive_count": 2}

    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    assert story["backend"] == "ollama"
    assert story["model"] == "deepseek-v4-flash:cloud"
    assert story["escalated"] is True
    assert "rework_attempts" not in story
    assert "review_inconclusive_count" not in story


def test_escalate_review_to_claude_default_preserves_claude_no_model(monkeypatch):
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    story = {"backend": "local", "model": "gemma4:26b-a4b-it-qat",
             "rework_attempts": 3}

    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    assert story["backend"] == "claude"
    assert story["escalated"] is True
    # default target has no model -> existing model field is left untouched
    assert story.get("model") == "gemma4:26b-a4b-it-qat"


def test_escalate_review_to_claude_clears_stale_review_state(monkeypatch):
    """_escalate_review_to_claude keeps the SAME worktree/branch (unlike
    _escalate_to_claude), so a stale last_reviewed_sha from before escalation
    remains valid git history. The Mode 24/28 guard in pipeline/server.py
    downgrades a fresh reviewer APPROVE back to REQUEST_CHANGES whenever a
    file recorded in story["last_review_findings"] does not appear in
    `git diff --name-only <last_reviewed_sha> HEAD` - which would perpetually
    re-trip on findings already fixed (the flagged file may never need
    touching again), silently burning the 'fresh' budget and parking an
    already-correct implementation. Escalation must therefore also pop the
    stale review-state keys, not just the rework/inconclusive counters."""
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")
    story = {
        "backend": "local",
        "model": "gemma4:26b-a4b-it-qat",
        "rework_attempts": 3,
        "review_inconclusive_count": 2,
        "last_review_findings": ["pipeline/server.py:3868 missing guard reset"],
        "last_reviewed_sha": "abc123deadbeef",
        "acceptance_failed_review": True,
        "review_feedback": "Prior Blocking finding(s) were never addressed",
    }

    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    # All six stale review-state keys must be cleared.
    assert "rework_attempts" not in story
    assert "review_inconclusive_count" not in story
    assert "last_review_findings" not in story
    assert "last_reviewed_sha" not in story
    assert "acceptance_failed_review" not in story
    assert "review_feedback" not in story
    # Existing retarget behavior must still hold in the same call.
    assert story["backend"] == "ollama"
    assert story["model"] == "deepseek-v4-flash:cloud"
    assert story["escalated"] is True


def test_escalate_review_to_claude_no_stale_keys_does_not_raise(monkeypatch):
    """A story that never had any of the six review-state keys set must not
    raise when escalated - dict.pop with a default already handles a missing
    key, so this documents no regression."""
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    story = {"backend": "local", "model": "gemma4:26b-a4b-it-qat"}

    # Must not raise.
    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story.get("model") == "gemma4:26b-a4b-it-qat"
    # None of the six keys should have been introduced.
    for key in ("rework_attempts", "review_inconclusive_count",
                "last_review_findings", "last_reviewed_sha",
                "acceptance_failed_review", "review_feedback"):
        assert key not in story


def test_check_story_status_routes_oracle_step_cap_to_interrupted(
    plan_dir, tmp_path, monkeypatch,
):
    """The oracle agent uses a different marker. It must be classified the
    same way: interrupted, not tests_passed, no test run."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        f"{_STEP_CAP_MARKER_ORACLE}\n"
    )
    _write_manifest(plan_dir, "cap2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on an oracle step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="cafe0000"))

    result = p.check_story_status("cap2", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "step_cap_reached"
    manifest = _read_manifest(plan_dir, "cap2")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    assert manifest["stories"]["S1"]["last_commit"] == "cafe0000"
    journal = p._read_journal("cap2", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_step_cap_streak_ignored_without_fallback_configured(
    plan_dir, tmp_path, monkeypatch,
):
    """A plan with no manifest["local_model_fallback"] (the default for every
    plan except the ones that opt in) must not track or act on a step-cap
    streak at all - existing behavior for the vast majority of plans is
    unchanged. Pinned to non-auto dispatch: under PIPELINE_BACKEND_DISPATCH=
    auto a no-fallback plan now escalates to Claude instead (see the
    escalates_to_claude_at_threshold test below), so this test's "no streak
    tracking at all" guarantee only holds outside auto mode - pin it
    explicitly rather than relying on the ambient shell env."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap3", "S1")

    story = _read_manifest(plan_dir, "cap3")["stories"]["S1"]
    assert "model" not in story  # never set - no fallback configured for this plan
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story


def test_check_story_status_step_cap_streak_increments_below_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """A plan opted into local_model_fallback tracks consecutive step-cap
    interrupts on the same model, but does not switch until the threshold
    (default 3) is reached."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap4", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "dispatched_model": "gpt-oss:20b", "step_cap_streak": 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap4.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap4", "S1")

    story = _read_manifest(plan_dir, "cap4")["stories"]["S1"]
    assert story["model"] == "gpt-oss:20b"  # not switched yet
    assert story["step_cap_streak"] == 2
    assert story["step_cap_streak_model"] == "gpt-oss:20b"


def test_check_story_status_step_cap_streak_switches_model_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """Once the step-cap streak on the same model reaches
    STEP_CAP_FALLBACK_THRESHOLD, the story's model switches to the plan's
    fallback model for the next resume - backend is untouched (stays local,
    never Claude), and the streak counters reset."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap5", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap5.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap5", "S1")

    story = _read_manifest(plan_dir, "cap5")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"  # never claude
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    notif = (plan_dir / "cap5.notifications.log").read_text()
    assert "switching to fallback model glm-5.2:cloud" in notif


def test_check_story_status_step_cap_streak_noop_once_already_on_fallback_model(
    plan_dir, tmp_path, monkeypatch,
):
    """A story already running on the plan's fallback model that keeps
    hitting the step cap must not restart the streak counters or fire
    another switch-model notification - there is no fallback past the
    fallback, so the guard (current model == fallback model) must hold."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap6", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "glm-5.2:cloud",
               "backend": "local", "dispatched_model": "glm-5.2:cloud"},
    })
    manifest_path = plan_dir / "cap6.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap6", "S1")

    story = _read_manifest(plan_dir, "cap6")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    notif_path = plan_dir / "cap6.notifications.log"
    assert not notif_path.exists() or "switching to fallback model" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_ignores_claude_backend_story(
    plan_dir, tmp_path, monkeypatch,
):
    """The step-cap streak fallback only ever applies to a local-backend
    story. A story dispatched on backend="claude" must never have its model
    switched by this logic, even if a plan opts into local_model_fallback and
    the streak threshold is reached (defense in depth alongside the local
    agent scripts being the only source of STEP_CAP_MARKERS)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap7", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "sonnet",
               "backend": "claude", "dispatched_model": "sonnet",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "sonnet"},
    })
    manifest_path = plan_dir / "cap7.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap7", "S1")

    story = _read_manifest(plan_dir, "cap7")["stories"]["S1"]
    assert story["model"] == "sonnet"  # never switched
    assert story["backend"] == "claude"
    notif_path = plan_dir / "cap7.notifications.log"
    assert not notif_path.exists() or "switching to fallback model" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_escalates_to_claude_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """Under PIPELINE_BACKEND_DISPATCH=auto, a plan with NO
    local_model_fallback configured must not spin forever on a struggling
    local model: once the step-cap streak reaches STEP_CAP_FALLBACK_THRESHOLD
    the story escalates to Claude via the same clean-slate teardown
    _escalate_to_claude already performs for test-failure escalation
    (worktree/branch removed, journal cleared, backend flips to claude,
    status reset to todo) - and check_story_status returns early with the
    step-cap-specific reason instead of reporting stale 'interrupted'."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap8", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap8", "S1")

    assert result == {"status": "todo", "reason": "step_cap_escalated_to_claude", "pid": 4242}
    story = _read_manifest(plan_dir, "cap8")["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls)
    journal_path = plan_dir / "cap8.S1.journal.json"
    assert not journal_path.exists()
    notif = (plan_dir / "cap8.notifications.log").read_text()
    assert "Claude" in notif
    assert "step cap" in notif.lower()


def test_check_story_status_step_cap_streak_below_threshold_no_claude_escalation(
    plan_dir, tmp_path, monkeypatch,
):
    """Same setup as the threshold-crossing test, but the streak has not yet
    reached STEP_CAP_FALLBACK_THRESHOLD: the story must stay 'interrupted',
    backend must be untouched, the streak fields must be incremented (never
    reset), and no teardown or notification must fire."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap9", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": 1, "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap9", "S1")

    assert result["status"] == "interrupted"
    story = _read_manifest(plan_dir, "cap9")["stories"]["S1"]
    assert story["backend"] == "local"
    assert story["step_cap_streak"] == 2
    assert story["step_cap_streak_model"] == "gpt-oss:20b"
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)
    notif_path = plan_dir / "cap9.notifications.log"
    assert not notif_path.exists() or "escalating to Claude" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_local_fallback_takes_priority_over_claude(
    plan_dir, tmp_path, monkeypatch,
):
    """Regression guard: local_model_fallback and Claude escalation are
    mutually exclusive with no chaining. When a plan has opted into
    local_model_fallback, crossing the threshold under
    PIPELINE_BACKEND_DISPATCH=auto must still route through the existing
    local-fallback-model switch, never through Claude escalation."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap10", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap10.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap10", "S1")

    story = _read_manifest(plan_dir, "cap10")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"
    assert "escalated" not in story
    assert result["status"] == "interrupted"


def test_check_story_status_step_cap_streak_local_fallback_never_escalates_to_claude(
    plan_dir, tmp_path, monkeypatch,
):
    """Sibling to test_check_story_status_step_cap_streak_noop_once_already_on_
    fallback_model: even when the streak on the fallback model itself is
    already several multiples past STEP_CAP_FALLBACK_THRESHOLD (i.e. the
    fallback model keeps step-capping too) and PIPELINE_BACKEND_DISPATCH=
    auto, a plan with local_model_fallback configured must never escalate to
    Claude - no chaining from local fallback to Claude."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap11", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "glm-5.2:cloud",
               "backend": "local", "dispatched_model": "glm-5.2:cloud",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD * 5,
               "step_cap_streak_model": "glm-5.2:cloud"},
    })
    manifest_path = plan_dir / "cap11.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap11", "S1")

    story = _read_manifest(plan_dir, "cap11")["stories"]["S1"]
    assert story["backend"] == "local"
    assert story.get("model") == "glm-5.2:cloud"
    assert "escalated" not in story
    assert result["status"] == "interrupted"
    assert story["step_cap_streak"] == p.STEP_CAP_FALLBACK_THRESHOLD * 5
    assert story["step_cap_streak_model"] == "glm-5.2:cloud"


@pytest.mark.parametrize("dispatch_env", ["local", "claude", None])
def test_check_story_status_step_cap_streak_noop_without_auto_dispatch(
    plan_dir, tmp_path, monkeypatch, dispatch_env,
):
    """No local_model_fallback configured AND PIPELINE_BACKEND_DISPATCH is
    not 'auto' (explicit 'local', explicit 'claude', or unset/default): the
    new Claude-escalation branch must never fire, and today's byte-for-byte
    behavior is preserved - the streak fields are not even created."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap12", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "backend": "local",
               "dispatched_model": "gpt-oss:20b"},
    })
    if dispatch_env is None:
        monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", dispatch_env)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap12", "S1")

    story = _read_manifest(plan_dir, "cap12")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert story["backend"] == "local"


def test_check_story_status_step_cap_streak_does_not_reescalate_already_escalated_story(
    plan_dir, tmp_path, monkeypatch,
):
    """Mirrors test_advance_pipeline_does_not_escalate_already_escalated's
    invariant for the step-cap streak path: a story with escalated=True must
    never be escalated a second time, even past the threshold. Uses a
    backend='local', escalated=True fixture explicitly (not inferred from
    backend=='claude')."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap13", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "escalated": True,
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap13", "S1")

    story = _read_manifest(plan_dir, "cap13")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert story["backend"] == "local"
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_check_story_status_step_cap_streak_ignores_claude_backend_without_fallback_configured(
    plan_dir, tmp_path, monkeypatch,
):
    """Extends test_check_story_status_step_cap_streak_ignores_claude_backend_
    story to the no-fallback-configured case: a story already on backend=
    'claude' with streak fields present must never enter the new
    Claude-escalation branch, even under PIPELINE_BACKEND_DISPATCH=auto with
    no local_model_fallback configured (defense in depth - STEP_CAP_MARKERS
    are only ever printed by local agent scripts)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap14", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "sonnet",
               "backend": "claude", "dispatched_model": "sonnet",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "sonnet"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap14", "S1")

    story = _read_manifest(plan_dir, "cap14")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert story["backend"] == "claude"
    assert story["step_cap_streak"] == p.STEP_CAP_FALLBACK_THRESHOLD - 1
    assert story["step_cap_streak_model"] == "sonnet"


def test_escalate_to_claude_pops_step_cap_streak_fields(
    plan_dir, tmp_path, monkeypatch,
):
    """_escalate_to_claude is now also invoked from the step-cap streak path
    (see check_story_status), in addition to its original test-failure
    caller. Its existing pop-key teardown must additionally clear
    step_cap_streak / step_cap_streak_model so stale local-streak state never
    lingers on a story that has moved to the claude backend."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escg.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "step_cap_streak": 3, "step_cap_streak_model": "gpt-oss:20b",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p._escalate_to_claude(manifest, "escg", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"


def test_check_story_status_normal_completion_still_routes_to_tests_passed(
    plan_dir, tmp_path, monkeypatch,
):
    """Negative case: a normal agent run whose last log line is NOT the
    step-cap marker must continue to route through the test suite and land
    on tests_passed. This is the regression guard against over-broad marker
    detection (substring-in-whole-file would have caught this case too, but
    we want to be explicit)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n")
    _write_manifest(plan_dir, "normal1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("normal1", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "normal1")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


def test_check_story_status_successful_dispatch_clears_both_streaks(
    plan_dir, tmp_path, monkeypatch,
):
    """Both streak counters describe CONSECUTIVE failures, but neither was
    ever cleared on a successful dispatch (only dispatch_attempts was) - so
    two infra deaths early plus one much later escalated as "3 consecutive"
    even with real progress in between. A dispatch that produced output and
    ran its tests breaks any streak, so both must reset here."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n")
    _write_manifest(plan_dir, "streakclear", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree),
               "step_cap_streak": 2, "step_cap_streak_model": "gpt-oss:20b",
               "infra_failure_streak": 2, "infra_failure_streak_model": "gpt-oss:20b",
               "dispatch_attempts": 1},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    p.check_story_status("streakclear", "S1")

    story = _read_manifest(plan_dir, "streakclear")["stories"]["S1"]
    assert "dispatch_attempts" not in story
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story


def test_check_story_status_step_cap_clean_worktree_no_crash(
    plan_dir, tmp_path, monkeypatch,
):
    """Boundary: marker present but the worktree is already clean (the
    agent hit the cap right after its own WIP commit, so there's nothing
    extra to checkpoint). The routing must still fire without crashing,
    using HEAD as the checkpoint sha."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "some prior output\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap3", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="clean000"))

    result = p.check_story_status("cap3", "S1")
    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "cap3")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    assert manifest["stories"]["S1"]["last_commit"] == "clean000"
    journal = p._read_journal("cap3", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_ignores_old_marker_then_normal_done(
    plan_dir, tmp_path, monkeypatch,
):
    """Boundary: on a resume the log is appended to, so a prior step-cap
    marker from a previous tick may appear earlier in the file. The
    classification must look ONLY at the last non-empty line, so the
    later successful done marker wins and the story routes to tests_passed
    like any other normal run."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        f"resume tick 1...\n{_STEP_CAP_MARKER_LOCAL}\n"
        "resume tick 2...\nDone.\n"
    )
    _write_manifest(plan_dir, "resume1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "ok"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("resume1", "S1")
    # Old marker must not poison the routing — last line is "Done.", which
    # is a normal completion.
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "resume1")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"




def test_dispatch_story_fresh_creates_worktree_and_dispatches(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "ds", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds", "S1")

    assert result["ok"] is True
    assert result["pid"] == 1234
    assert result["resumed"] is False
    assert ["git", "worktree", "add", "-b", "agent/s1", str(worktree_root / "S1"),
            "origin/main"] in run_calls
    assert ["git", "fetch", "origin", "main"] in run_calls
    assert not any(c[:2] == ["git", "pull"] for c in run_calls)

    manifest = _read_manifest(plan_dir, "ds")
    story = manifest["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 1234
    assert story["worktree"] == str(worktree_root / "S1")


def test_dispatch_story_passes_rework_full_suite_when_ci_rework_set(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """L1 threading: a story carrying `ci_rework` (set by the merge-CI rework
    router) must reach the agent subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1
    so the harness raises the done-bar to full-suite-green on the redispatch."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    # The story below carries an `acceptance` block, so dispatch_story's
    # pre-dispatch oracle gate also calls subprocess.run (unlike the
    # fire-and-forget git calls elsewhere in dispatch_story, it reads
    # .returncode/.stdout) - give it a real CompletedProcess so the gate
    # classifies this as an ordinary not-yet-implemented failure rather than
    # a broken oracle.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4321),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "cirs", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "ci_rework": True,
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("cirs", "S1")
    assert result["ok"] is True
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_story_omits_rework_full_suite_without_ci_rework(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard: a fresh dispatch (no ci_rework) must NOT set
    LOCAL_AGENT_REWORK_FULL_SUITE, or the full-suite done-bar would silently
    apply to cold-start dispatches."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    # See the sibling test above: this story also carries an `acceptance`
    # block, so the pre-dispatch oracle gate needs a real CompletedProcess
    # from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4322),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "cirsnone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [],
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("cirsnone", "S1")
    assert result["ok"] is True
    assert "LOCAL_AGENT_REWORK_FULL_SUITE" not in captured["env"]


