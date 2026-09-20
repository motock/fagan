"""TDD suite: escalation must record the tier a story was FIRST dispatched on.

ROOT CAUSE
----------
``pipeline/escalation.py``'s ``_escalate_to_claude`` and
``_escalate_review_to_claude`` overwrite ``story["backend"]`` / ``story["model"]``
with the escalation TARGET. Nothing records the tier the story was first
dispatched on, so ``pipeline/local_success.py`` reads an escalated story as an
escalation-tier story: the tier that actually failed is erased and the
escalation tier is charged with a failure that was not its own. That
attribution is what the first-pass-clean metric is built from.

CONTRACT PINNED BY THIS FILE
----------------------------
* A new module-level helper ``_stamp_first_dispatch(story) -> None`` records
  ``pre_escalation_backend`` / ``pre_escalation_model`` BEFORE the escalation
  target overwrites ``backend`` / ``model`` / ``dispatched_model``.
* The stamp is sticky (``setdefault``): a second escalation never overwrites
  the original tier.
* ``pre_escalation_model`` falls back to ``story["model"]`` when
  ``dispatched_model`` is absent, and is ``None`` (key PRESENT) for a story
  with no tier fields at all.
* The helper is defined immediately BEFORE ``_escalation_repo_root`` (which is
  itself immediately before ``_escalate_to_claude``), and is invoked exactly
  twice - once in each escalation function, as the first line of the target
  write.
* ``_escalate_to_local_fallback_model`` stays on the local tier and must NOT
  stamp.
* The survivor bookkeeping (pid/worktree/dispatch_attempts teardown, status
  reset, escalated flag, manifest write) is untouched.

Every test stubs the git boundary (``pipeline.escalation.subprocess``) and
isolates PLAN_DIR / notifications, so nothing here touches this machine's real
configuration and no real git is ever shelled out.
"""

from __future__ import annotations

import ast
import json
import subprocess as real_subprocess
from pathlib import Path

import pytest

from pipeline import escalation as esc
from pipeline import server


class _FakeSubprocess:
    """Stand-in for the ``subprocess`` module; never spawns a real process."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return real_subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeSubprocess()
    monkeypatch.setattr(esc, "subprocess", fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_plan_dir(monkeypatch, tmp_path):
    """Keep the journal path and notifications out of this machine's real dirs.

    The escalation target is pinned via the escalation-specific env override so
    nothing here depends on this machine's real model registry.
    """
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(esc, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "claude")
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)


def _manifest(story_key: str, **story_overrides) -> dict:
    """A failed local story, exactly the shape escalation is handed.

    ``worktree`` is deliberately empty: ``_resolve_story_branch`` then probes a
    non-existent cwd, fails open to the convention branch, and never spawns a
    real git process.
    """
    story = {
        "summary": "thing",
        "status": "failed",
        "pid": 4242,
        "worktree": "",
        "backend": "local",
        "model": "gpt-oss-20b-high",
        "dispatched_model": "gpt-oss-20b-high",
        "dispatch_attempts": 1,
        "dispatch_error": "boom",
        "step_cap_streak": 2,
        "infra_failure_streak": 1,
    }
    story.update(story_overrides)
    return {"epics": {}, "stories": {story_key: story}}


def _stamp():
    fn = getattr(esc, "_stamp_first_dispatch", None)
    assert fn is not None, (
        "pipeline.escalation._stamp_first_dispatch(story) is missing: escalation "
        "must record the tier a story was FIRST dispatched on (pre_escalation_"
        "backend / pre_escalation_model) before it overwrites backend/model with "
        "the escalation target"
    )
    return fn


def _module_source() -> str:
    return Path(esc.__file__).read_text(encoding="utf-8")


def _module_level_function_names() -> list[str]:
    tree = ast.parse(_module_source())
    return [
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    ]


# --------------------------------------------------------------------------
# 1. dispatch escalation stamps the local tier before overwriting it
# --------------------------------------------------------------------------
def test_dispatch_escalation_stamps_the_local_tier_before_overwriting_it(
    fake_git, tmp_path
):
    manifest = _manifest("S1")
    story = manifest["stories"]["S1"]
    manifest_path = tmp_path / "plan.json"

    esc._escalate_to_claude(manifest, "plan", "S1", manifest_path)

    assert story["pre_escalation_backend"] == "local"
    assert story["pre_escalation_model"] == "gpt-oss-20b-high"
    # The escalation target still wins the live fields.
    assert story["backend"] == "claude"

    # Persisted, not just set in memory.
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    persisted = written["stories"]["S1"]
    assert persisted["pre_escalation_backend"] == "local"
    assert persisted["pre_escalation_model"] == "gpt-oss-20b-high"
    assert persisted["backend"] == "claude"


# --------------------------------------------------------------------------
# 2. review escalation stamps the local tier before overwriting it
# --------------------------------------------------------------------------
def test_review_escalation_stamps_the_local_tier_before_overwriting_it(fake_git):
    story = {
        "status": "failed",
        "backend": "local",
        "model": "gpt-oss-20b-high",
        "dispatched_model": "gpt-oss-20b-high",
        "rework_attempts": 3,
        "review_inconclusive_count": 2,
    }

    esc._escalate_review_to_claude(story, "S2", "plan", "budget exhausted")

    assert story["pre_escalation_backend"] == "local"
    assert story["pre_escalation_model"] == "gpt-oss-20b-high"
    assert story["backend"] == "claude"


# --------------------------------------------------------------------------
# 3. a second escalation does not overwrite the first stamp (setdefault sticky)
# --------------------------------------------------------------------------
def test_a_second_escalation_does_not_overwrite_the_first_stamp(fake_git, tmp_path):
    manifest = _manifest("S3")
    story = manifest["stories"]["S3"]
    manifest_path = tmp_path / "plan.json"

    _stamp()(story)
    assert story["pre_escalation_backend"] == "local"
    assert story["pre_escalation_model"] == "gpt-oss-20b-high"

    # A later round re-dispatched on a different tier, then escalated again.
    story["backend"] = "ollama"
    story["model"] = "deepseek-v4-flash:cloud"
    story["dispatched_model"] = "deepseek-v4-flash:cloud"

    esc._escalate_to_claude(manifest, "plan", "S3", manifest_path)

    assert story["pre_escalation_backend"] == "local"
    assert story["pre_escalation_model"] == "gpt-oss-20b-high"


# --------------------------------------------------------------------------
# 4. stamp falls back to model when dispatched_model is absent
# --------------------------------------------------------------------------
def test_stamp_falls_back_to_model_when_dispatched_model_is_absent():
    story = {"backend": "local", "model": "qwen3-30b"}

    _stamp()(story)

    assert story["pre_escalation_backend"] == "local"
    assert story["pre_escalation_model"] == "qwen3-30b"


# --------------------------------------------------------------------------
# 5. a story with no tier fields stamps None (keys PRESENT), never raises
# --------------------------------------------------------------------------
def test_stamp_of_a_story_with_no_tier_fields_is_none_not_missing():
    story: dict = {}

    _stamp()(story)

    assert "pre_escalation_backend" in story
    assert "pre_escalation_model" in story
    assert story["pre_escalation_backend"] is None
    assert story["pre_escalation_model"] is None


# --------------------------------------------------------------------------
# 6. the local fallback model stays on the local tier and must NOT stamp
# --------------------------------------------------------------------------
def test_local_fallback_model_escalation_does_not_stamp(fake_git, tmp_path):
    manifest = _manifest("S6")
    story = manifest["stories"]["S6"]
    manifest_path = tmp_path / "plan.json"

    esc._escalate_to_local_fallback_model(
        manifest, "plan", "S6", manifest_path, "gemma4:26b-a4b-it-qat"
    )

    assert "pre_escalation_backend" not in story
    assert "pre_escalation_model" not in story


# --------------------------------------------------------------------------
# 7. helper is module-level and keeps the repo-root adjacency
# --------------------------------------------------------------------------
def test_helper_is_module_level_and_keeps_the_repo_root_adjacency():
    src = _module_source()
    names = _module_level_function_names()

    assert "_stamp_first_dispatch" in names, (
        "pipeline/escalation.py must define a MODULE-LEVEL "
        "_stamp_first_dispatch function"
    )
    idx = names.index("_stamp_first_dispatch")
    assert names[idx + 1] == "_escalation_repo_root", (
        "_stamp_first_dispatch must be defined immediately BEFORE "
        "_escalation_repo_root (which stays immediately before "
        f"_escalate_to_claude); found {names[idx + 1]!r} after it"
    )
    assert src.count("_stamp_first_dispatch(story)") == 2, (
        "both escalation call sites (_escalate_to_claude and "
        "_escalate_review_to_claude) must invoke _stamp_first_dispatch(story) "
        "exactly once each"
    )


# --------------------------------------------------------------------------
# 8. the survivor bookkeeping still happens
# --------------------------------------------------------------------------
def test_survivor_bookkeeping_still_happens(fake_git, tmp_path):
    manifest = _manifest("S8")
    story = manifest["stories"]["S8"]
    manifest_path = tmp_path / "plan.json"

    esc._escalate_to_claude(manifest, "plan", "S8", manifest_path)

    assert "pid" not in story
    assert "worktree" not in story
    assert "dispatch_attempts" not in story
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert manifest_path.exists()
