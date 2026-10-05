"""Unit tests for harness.py's per-arm plan role_config / story model pin.

A bench arm's model only actually runs when the *plan* pins it: role
resolution priority is plan role_config -> registry["roles"][role] ->
PIPELINE_BACKEND_<ROLE> env var (app/role_registry.resolve_role), so the
live registry's roles.* pins silently beat every PIPELINE_BACKEND_* /
PIPELINE_LOCAL_MODEL_DEFAULT an arm's env sets. Measured 2026-10-01: a
gemma4_26b cell booted as `claude-sonnet-5-5` and died on a Claude session
limit, because model_registry.local.json pins roles.dispatch=claude.

The second half is the story's own ``model`` pin: _resolve_dispatch_target
returns story["model"] unconditionally, so every task's spec.json
``"model": "sonnet"`` would be handed to the Ollama driver as a literal
model name even once the plan's role_config wins.

Run: pytest tests/benchmark/test_harness_role_config.py
"""
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent

if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness
from models import MODELS


def _plan(tmp_path, **kwargs):
    task = harness.load_task("lru_cache")
    return harness.build_plan(tmp_path, task, **kwargs)


def test_build_plan_omits_role_config_by_default(tmp_path):
    assert "role_config" not in _plan(tmp_path)


def test_build_plan_carries_the_arms_role_config(tmp_path):
    rc = {"dispatch": {"provider": "ollama", "model": "deepseek-v4.1-flash"}}
    assert _plan(tmp_path, plan_role_config=rc)["role_config"] == rc


def test_build_plan_keeps_the_spec_model_pin_by_default(tmp_path):
    story = _plan(tmp_path)["epics"][0]["stories"][0]
    assert story["model"] == "sonnet"


def test_build_plan_replaces_the_spec_model_pin_when_the_arm_names_one(tmp_path):
    story = _plan(tmp_path, story_model="deepseek-v4.1-flash")["epics"][0]["stories"][0]
    assert story["model"] == "deepseek-v4.1-flash"


def test_the_gemma_arm_role_config_reaches_the_built_plan(tmp_path):
    arm = MODELS["gemma4_26b"]
    plan = _plan(tmp_path, plan_role_config=arm["role_config"],
                 story_model=arm["story_model"])
    assert plan["role_config"]["dispatch"] == {"provider": "ollama", "model": "gemma4-26b-qat"}
    assert plan["epics"][0]["stories"][0]["model"] == "gemma4-26b-qat"


def test_gemma_arm_holds_every_non_dispatch_role_at_the_baseline_reviewer():
    """Only dispatch may differ from the baseline, or a scorecard difference
    is not attributable to the implementing model."""
    rc = MODELS["gemma4_26b"]["role_config"]
    assert set(rc) == {"planner", "dispatch", "test_author", "review", "overlord"}
    assert rc["dispatch"] == {"provider": "ollama", "model": "gemma4-26b-qat"}
    for role in ("planner", "test_author", "review", "overlord"):
        assert rc[role] == {"provider": "ollama", "model": "deepseek-v4.1-flash"}


def test_gemma_arm_matches_gptoss_high_outside_its_model_pin():
    arm, gptoss = dict(MODELS["gemma4_26b"]), dict(MODELS["gptoss_high"])
    for cfg in (arm, gptoss):
        cfg.pop("role_config", None)
        cfg.pop("story_model", None)
        cfg["env"] = {k: v for k, v in cfg["env"].items()
                      if k != "PIPELINE_LOCAL_MODEL_DEFAULT"}
    assert arm == gptoss
