"""Acceptance oracle: diagnose_failure must turn failure evidence into a root
cause via a configurable role, and must FAIL OPEN (return None) on every error
path - a diagnosis is an optimization, never a gate.
"""
from pipeline import rebrief


def test_returns_the_role_output_as_the_diagnosis(monkeypatch):
    monkeypatch.setattr(
        rebrief, "_run_diagnosis_role", lambda *a, **k: "TEMPLATE_DIR came from --repo-root"
    )
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) == (
        "TEMPLATE_DIR came from --repo-root"
    )


def test_unconfigured_role_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: None)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_role_exception_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_role_output_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: "   ")
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_evidence_skips_the_role_entirely(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the role with no evidence")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("", {"summary": "s"}) is None


# --- local-backend default for an unconfigured diagnosis role ---
# The "diagnosis" role ships unconfigured (no model_registry entry, no env), so
# without a default _run_diagnosis_role returns None and every rebrief is a blind
# retry. The default reuses the story's OWN local backend+model — the model that
# ran the struggle is the cheapest sensible diagnoser, and since step-cap only
# fires on local backends this never surprises an operator with Claude spend.


class _RecordingDriver:
    def __init__(self):
        self.complete_kwargs = {}

    def complete(self, *, prompt, system, model):
        self.complete_kwargs = {"prompt": prompt, "system": system, "model": model}
        return "ROOT CAUSE: the guard rejects legitimate appends."


def _no_diagnosis_role(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})


def test_unconfigured_role_falls_back_to_story_local_backend(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: driver)

    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    assert rebrief._run_diagnosis_role("evidence", story) == (
        "ROOT CAUSE: the guard rejects legitimate appends."
    )
    # The diagnoser is the story's own backend+concrete model, not a default tier.
    assert driver.complete_kwargs["model"] == "gpt-oss:20b"
    assert "evidence" in driver.complete_kwargs["prompt"]


def test_unconfigured_role_uses_declared_model_when_no_dispatched_model(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: driver)

    story = {"summary": "s", "backend": "local", "model": "devstral"}
    assert rebrief._run_diagnosis_role("evidence", story) == "ROOT CAUSE: the guard rejects legitimate appends."
    assert driver.complete_kwargs["model"] == "devstral"


def test_unconfigured_role_fails_open_for_claude_backend(monkeypatch):
    """A claude-backend story must NOT silently spend Claude on a diagnosis by
    default — fail open (None) so the retry is a plain resume, not a surprise bill."""
    _no_diagnosis_role(monkeypatch)

    def boom(role, name=None):
        raise AssertionError("must not dispatch a Claude diagnosis by default")
    monkeypatch.setattr(rebrief.backend, "get_backend", boom)

    story = {"summary": "s", "backend": "claude", "dispatched_model": "sonnet"}
    assert rebrief._run_diagnosis_role("evidence", story) is None


def test_unconfigured_role_fails_open_for_auto_backend(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: (_ for _ in ()).throw(
                            AssertionError("auto must be resolved by the caller")))
    assert rebrief._run_diagnosis_role("evidence", {"summary": "s", "backend": "auto"}) is None


def test_unconfigured_role_fails_open_when_no_model(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: (_ for _ in ()).throw(AssertionError("no model")))
    assert rebrief._run_diagnosis_role("evidence", {"summary": "s", "backend": "ollama"}) is None


def test_explicit_diagnosis_env_wins_over_story_backend_default(monkeypatch):
    """An operator who set PIPELINE_BACKEND_DIAGNOSIS keeps getting that provider,
    not the story-backend default."""
    monkeypatch.setenv("PIPELINE_BACKEND_DIAGNOSIS", "mlx")
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    from app.role_registry import RoleResolution
    monkeypatch.setattr(rebrief.role_registry, "resolve_role",
                        lambda role, **k: RoleResolution(provider="mlx", model="qwen:30b"))
    seen = {}

    def fake_get_backend(role, name=None):
        seen["name"] = name
        return _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", fake_get_backend)

    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    assert seen["name"] == "mlx"


# --- the ORIGINAL BRIEF must reach the diagnosis prompt ---
# Observed live 2026-09-17 (story 146753bb): the brief pre-authorized a specific
# test re-pin, but the diagnosis role only ever saw the evidence (diff/log facts)
# and invented a conflicting fix, which the next attempt then followed. The
# diagnosis model must be given the chance to see what the story was ORIGINALLY
# asked to do, so it cannot contradict an already pre-authorized edit.


def test_original_brief_excerpt_labels_and_includes_the_instructions():
    story = {"agent_instructions": "Re-pin FOO_HASH to the new digest."}
    excerpt = rebrief._original_brief_excerpt(story)
    assert "ORIGINAL BRIEF" in excerpt
    assert "Re-pin FOO_HASH to the new digest." in excerpt


def test_original_brief_excerpt_is_empty_without_instructions():
    assert rebrief._original_brief_excerpt({}) == ""
    assert rebrief._original_brief_excerpt({"agent_instructions": ""}) == ""
    assert rebrief._original_brief_excerpt({"agent_instructions": "   \n  "}) == ""
    assert rebrief._original_brief_excerpt({"agent_instructions": None}) == ""


def test_original_brief_excerpt_truncates_a_long_brief():
    long_brief = "A" * (rebrief._ORIGINAL_BRIEF_EXCERPT_LIMIT + 500)
    excerpt = rebrief._original_brief_excerpt({"agent_instructions": long_brief})
    assert "A" * rebrief._ORIGINAL_BRIEF_EXCERPT_LIMIT in excerpt
    assert "A" * (rebrief._ORIGINAL_BRIEF_EXCERPT_LIMIT + 1) not in excerpt


def test_original_brief_excerpt_keeps_the_head_not_the_tail():
    """Pre-authorized edits are stated explicitly and early, so the HEAD is what
    matters here (contrast with the log/evidence tails elsewhere in the module)."""
    head = "PRE-AUTHORIZED: re-pin FOO_HASH."
    tail = "B" * (rebrief._ORIGINAL_BRIEF_EXCERPT_LIMIT + 100) + "TAIL-MARKER"
    excerpt = rebrief._original_brief_excerpt({"agent_instructions": head + tail})
    assert head in excerpt
    assert "TAIL-MARKER" not in excerpt


def test_diagnosis_prompt_includes_the_original_brief_for_the_story_backend_default(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)

    story = {
        "summary": "s",
        "backend": "ollama",
        "dispatched_model": "gpt-oss:20b",
        "agent_instructions": "Re-pin FOO_HASH to the new digest.",
    }
    rebrief._run_diagnosis_role("evidence", story)
    prompt = driver.complete_kwargs["prompt"]
    assert "ORIGINAL BRIEF (what this story was originally asked to do):" in prompt
    assert "Re-pin FOO_HASH to the new digest." in prompt
    # The evidence is still there, and the two concerns stay visually distinct.
    assert "evidence" in prompt


def test_diagnosis_prompt_includes_the_original_brief_for_the_provider_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DIAGNOSIS", "mlx")
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    from app.role_registry import RoleResolution
    monkeypatch.setattr(rebrief.role_registry, "resolve_role",
                        lambda role, **k: RoleResolution(provider="mlx", model="qwen:30b"))
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)

    story = {
        "summary": "s",
        "backend": "ollama",
        "dispatched_model": "gpt-oss:20b",
        "agent_instructions": "Re-pin FOO_HASH to the new digest.",
    }
    rebrief._run_diagnosis_role("evidence", story)
    prompt = driver.complete_kwargs["prompt"]
    assert "ORIGINAL BRIEF (what this story was originally asked to do):" in prompt
    assert "Re-pin FOO_HASH to the new digest." in prompt


def test_diagnosis_prompt_is_unchanged_when_there_is_no_original_brief(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)

    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    prompt = driver.complete_kwargs["prompt"]
    assert "ORIGINAL BRIEF (what this story was originally asked to do):" not in prompt
    assert prompt.endswith("evidence")


def test_diagnosis_prompt_carries_a_pre_authorized_instruction_verbatim(monkeypatch):
    """Regression guard for the 2026-09-17 contradiction: the literal
    pre-authorized instruction must be in the prompt, so the model was given the
    chance to see and follow it instead of inventing a conflicting fix."""
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)

    pre_authorized = (
        "Replace the value of SUFFIX_FROM_CONFIG_SHA256 with the printed digest; "
        "do NOT replace this check with something else."
    )
    story = {
        "summary": "s",
        "backend": "ollama",
        "dispatched_model": "gpt-oss:20b",
        "agent_instructions": pre_authorized,
    }
    rebrief._run_diagnosis_role("evidence", story)
    prompt = driver.complete_kwargs["prompt"]
    assert pre_authorized in prompt
    # And the model is told to defer to it rather than invent an alternative.
    assert "MUST follow it exactly" in prompt
