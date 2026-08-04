"""Acceptance oracle: the step-cap diagnosis prompt must steer the diagnoser
toward remediations the resumed local attempt can actually execute (targeted
reads / anchored edits), and explicitly away from git apply or a full-file
rewrite. Grounded in a live incident: the DASHBOARD_PROGRESS_TIER1 rebrief
(frontend-progress-bar story, 2026-08-04) correctly diagnosed a context-
trimming root cause but suggested `git apply`, which the resumed local
attempt has no way to run.

The prompt is built exactly once (a single `prompt = (...)` assignment) and
feeds BOTH the provider_override branch and the story-backend fallback
branch, so every assertion below is run against both call sites.
"""
from app.role_registry import RoleResolution
from pipeline import rebrief


class _RecordingDriver:
    def __init__(self):
        self.complete_kwargs = {}

    def complete(self, *, prompt, system, model):
        self.complete_kwargs = {"prompt": prompt, "system": system, "model": model}
        return "diagnosis"


# ---------------------------------------------------------------------------
# Substance checks. Each mechanically-checkable requirement from the story gets
# its own assertion so the implementer cannot ship a partial prompt.
# ---------------------------------------------------------------------------

def _assert_tool_guidance_substance(prompt: str) -> None:
    p = prompt.lower()
    # The next attempt's actual toolset is named: targeted/line-ranged reads +
    # search + anchored str_replace/replace_lines-style edits.
    assert "git apply" in p, "prompt must name the forbidden `git apply` tool"
    assert ("anchored" in p) or ("str_replace" in p), (
        "prompt must mention anchored edits / str_replace"
    )
    # Targeted reads / search are the available tools and must be recommended.
    assert ("read" in p) or ("search" in p), (
        "prompt must mention targeted reads or search as available tools"
    )
    # The prompt must forbid a full-file / whole-file rewrite.
    assert ("full-file" in p) or ("whole file" in p) or ("full file" in p), (
        "prompt must forbid a full-file / whole-file rewrite"
    )
    # The prompt must forbid an in-place re-indent of a large existing function.
    assert "re-indent" in p or "reindent" in p, (
        "prompt must forbid an in-place re-indent of a large existing function"
    )
    # The fix must be achievable with those tools: recommend targeted
    # reads/searches and small anchored edits (positive guidance present).
    assert "small" in p or "targeted" in p, (
        "prompt must recommend small/targeted edits as the achievable fix"
    )


def test_fallback_branch_prompt_has_full_tool_guidance(monkeypatch):
    """Story-backend fallback path: the prompt carries the full tool guidance."""
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)
    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    assert driver.complete_kwargs, "fallback branch must call the driver"
    _assert_tool_guidance_substance(driver.complete_kwargs["prompt"])


def test_override_branch_prompt_has_full_tool_guidance(monkeypatch):
    """provider_override path (env var): the SAME prompt carries the full tool
    guidance - the prompt is built once and feeds both branches."""
    monkeypatch.setenv("PIPELINE_BACKEND_DIAGNOSIS", "mlx")
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    monkeypatch.setattr(
        rebrief.role_registry, "resolve_role",
        lambda role, **k: RoleResolution(provider="mlx", model="qwen:30b"),
    )
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)
    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    assert driver.complete_kwargs, "override branch must call the driver"
    _assert_tool_guidance_substance(driver.complete_kwargs["prompt"])


def test_both_branches_share_one_prompt_construction(monkeypatch):
    """The prompt is built exactly once: both branches receive a prompt whose
    guidance text is identical (modulo the evidence payload)."""
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})

    fallback = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: fallback)
    rebrief._run_diagnosis_role("EVIDENCE-A", {"summary": "s", "backend": "ollama",
                                               "dispatched_model": "gpt-oss:20b"})
    fallback_prompt = fallback.complete_kwargs["prompt"].replace("EVIDENCE-A", "EVIDENCE")

    monkeypatch.setenv("PIPELINE_BACKEND_DIAGNOSIS", "mlx")
    monkeypatch.setattr(
        rebrief.role_registry, "resolve_role",
        lambda role, **k: RoleResolution(provider="mlx", model="qwen:30b"),
    )
    override = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: override)
    rebrief._run_diagnosis_role("EVIDENCE-B", {"summary": "s", "backend": "ollama",
                                               "dispatched_model": "gpt-oss:20b"})
    override_prompt = override.complete_kwargs["prompt"].replace("EVIDENCE-B", "EVIDENCE")

    assert fallback_prompt == override_prompt, (
        "prompt must be a single construction shared by both call sites"
    )


def test_diagnosis_prompt_still_includes_the_evidence(monkeypatch):
    """The guidance must not displace the evidence - it stays in the prompt."""
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)
    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("THE EVIDENCE TEXT", story)
    assert "THE EVIDENCE TEXT" in driver.complete_kwargs["prompt"]


def test_diagnosis_prompt_still_asks_for_root_cause_and_minimal_fix(monkeypatch):
    """The original ask (root cause + minimal fix) must survive the extension."""
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)
    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    p = driver.complete_kwargs["prompt"].lower()
    assert "root cause" in p
    assert "minimal" in p