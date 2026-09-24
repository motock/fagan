"""Escalation and fallback guards must fire for every local driver, not just "local".

Dispatch writes the concrete driver name to ``story["backend"]`` ("ollama",
"lmstudio", "mlx"), but nine streak/fallback/escalation guards in
story_status, advance and triage compared it to the literal ``"local"``. So an
on-device ollama story that hit the step cap never escalated: PRH-2
(2026-09-24) took five step-cap rebriefs over ~9 hours before a different path
finally moved it. The guards now test membership in ``_LOCAL_BACKEND_NAMES``.

Escalation wipes the worktree and restarts the story on the escalation
target, so an escalation guard must also skip a story that already runs on
that target (a cloud-OSS story pinned to the target model): escalating it
would only restart it on the same model.
"""

import inspect
import json

import pytest

from pipeline import advance, triage
from pipeline import server as p
from pipeline import story_status as ss
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _make_fake_git_run,
    _write_manifest,
    plan_dir,
)

ON_DEVICE = "gpt-oss-20b-high:latest"
TARGET = "glm-5.3-flash:cloud"
STEP_CAP_LINE = "[ended without done — step cap reached]"


@pytest.fixture
def escalation_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", TARGET)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(
        p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests"))
    )
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None)


def _step_capped_story(tmp_path, model, streak):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"[step 40] edit\n{STEP_CAP_LINE}\n")
    return {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
        "backend": "ollama",
        "model": model,
        "dispatched_model": model,
        "step_cap_streak": streak,
        "step_cap_streak_model": model,
    }


def _infra_story(tmp_path, model):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 5] LLM call failed after trim-retry: Server error '500'\n")
    return {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
        "backend": "ollama",
        "model": model,
        "dispatched_model": model,
        "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
        "infra_failure_streak_model": model,
    }


def test_an_ollama_story_at_the_step_cap_threshold_escalates(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, ON_DEVICE, p.STEP_CAP_FALLBACK_THRESHOLD - 1)
    _write_manifest(plans, "lbg1", {"S1": story})

    result = p.check_story_status("lbg1", "S1")

    assert result == {"status": "todo", "reason": "step_cap_escalated_to_claude", "pid": 4242}


def test_the_escalated_ollama_story_moves_to_the_target_model(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, ON_DEVICE, p.STEP_CAP_FALLBACK_THRESHOLD - 1)
    _write_manifest(plans, "lbg1", {"S1": story})

    p.check_story_status("lbg1", "S1")

    persisted = json.loads((plans / "lbg1.manifest.json").read_text())["stories"]["S1"]
    assert persisted["model"] == TARGET
    assert persisted["escalated"] is True


def test_a_story_already_on_the_target_is_not_escalated_at_the_step_cap(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, TARGET, p.STEP_CAP_FALLBACK_THRESHOLD - 1)
    _write_manifest(plans, "lbg1", {"S1": story})

    result = p.check_story_status("lbg1", "S1")

    assert result == {"status": "interrupted", "pid": 4242, "reason": "step_cap_reached"}


def test_a_story_already_on_the_target_keeps_its_worktree_record(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, TARGET, p.STEP_CAP_FALLBACK_THRESHOLD - 1)
    _write_manifest(plans, "lbg1", {"S1": story})

    p.check_story_status("lbg1", "S1")

    persisted = json.loads((plans / "lbg1.manifest.json").read_text())["stories"]["S1"]
    assert persisted["worktree"] == story["worktree"]
    assert "escalated" not in persisted


def test_an_ollama_story_at_the_infra_failure_threshold_escalates(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    _write_manifest(plans, "lbg1", {"S1": _infra_story(tmp_path, ON_DEVICE)})

    result = p.check_story_status("lbg1", "S1")

    assert result == {"status": "todo", "reason": "infra_failure_escalated_to_claude", "pid": 4242}


def test_a_story_already_on_the_target_is_not_escalated_on_infra_failures(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    _write_manifest(plans, "lbg1", {"S1": _infra_story(tmp_path, TARGET)})

    result = p.check_story_status("lbg1", "S1")

    assert result == {"status": "interrupted", "pid": 4242, "reason": "infra_failure"}


def test_an_mlx_story_below_the_threshold_just_resumes(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, ON_DEVICE, 0)
    story["backend"] = "mlx"
    _write_manifest(plans, "lbg1", {"S1": story})

    result = p.check_story_status("lbg1", "S1")

    assert result == {"status": "interrupted", "pid": 4242, "reason": "step_cap_reached"}


def test_an_mlx_story_counts_toward_the_step_cap_streak(request, tmp_path, escalation_env):
    plans = request.getfixturevalue("plan_dir")
    story = _step_capped_story(tmp_path, ON_DEVICE, 0)
    story["backend"] = "mlx"
    _write_manifest(plans, "lbg1", {"S1": story})

    p.check_story_status("lbg1", "S1")

    persisted = json.loads((plans / "lbg1.manifest.json").read_text())["stories"]["S1"]
    assert persisted["step_cap_streak"] == 1


@pytest.mark.parametrize("module", [ss, advance, triage], ids=["story_status", "advance", "triage"])
def test_no_backend_guard_compares_to_the_literal_local(module):
    source = inspect.getsource(module)

    assert 'story.get("backend", "local") == "local"' not in source
    assert 'story.get("backend") == "local"' not in source
