"""Regression tests: LiteLLM vendor/model strings pass through
_resolve_local_model unchanged.

Root cause encoded here: the function's first guard only treated a value
containing ':' (Ollama's tag separator) as an already-concrete model tag.
A LiteLLM model string like 'openai/gpt-5-mini' has no colon, so the guard
missed, the _LOCAL_TIER_ENV lookup returned None, and the function silently
substituted PIPELINE_LOCAL_MODEL_DEFAULT for the model the caller asked for.
Tier names are always bare lowercase words ('sonnet'/'opus'/'haiku'), so a
value containing ':' or '/' is always a concrete model tag, never a tier.
"""
from app import ollama_prompt_utils as opu

SENTINEL_DEFAULT = "SENTINEL-default-DO-NOT-RETURN"


def _patch_default(monkeypatch):
    """Pin the default so no test depends on the live host's config."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SENTINEL_DEFAULT)


def test_slash_model_string_passes_through_unchanged(monkeypatch):
    """T1: a LiteLLM vendor/model string is a concrete model tag, not a
    tier name - it must be returned exactly, never swapped for the default."""
    _patch_default(monkeypatch)
    assert opu._resolve_local_model("openai/gpt-5-mini") == "openai/gpt-5-mini"


def test_slash_model_string_beats_provider_scoped_tier_env(monkeypatch):
    """T2: a concrete model string is never overridden by a tier env var,
    even when the provider-scoped one is set."""
    _patch_default(monkeypatch)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_LITELLM_SONNET", "litellm-sonnet-override:7")
    assert opu._resolve_local_model(
        "anthropic/claude-sonnet-4", provider="litellm"
    ) == "anthropic/claude-sonnet-4"


def test_colon_tag_still_passes_through_unchanged(monkeypatch):
    """T3 regression guard: the pre-existing ':' branch is intact - a
    fall-through to the sentinel default would be unmistakable."""
    _patch_default(monkeypatch)
    assert opu._resolve_local_model("glm-5.3-flash:cloud") == "glm-5.3-flash:cloud"


def test_bare_tier_name_still_resolves_through_env_table(monkeypatch):
    """T4 regression guard: a bare tier name (no ':' or '/') must NOT trip
    the concrete-tag guard and still resolves through the env table."""
    _patch_default(monkeypatch)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_OLLAMA_SONNET", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "stub-tag:1")
    assert opu._resolve_local_model("sonnet") == "stub-tag:1"


def test_unknown_bare_word_falls_back_to_default(monkeypatch):
    """T5 negative: an unknown bare word with neither ':' nor '/' still
    falls through to PIPELINE_LOCAL_MODEL_DEFAULT."""
    _patch_default(monkeypatch)
    assert opu._resolve_local_model("nonsense") == SENTINEL_DEFAULT


def test_empty_string_falls_back_to_default_without_raising(monkeypatch):
    """T6 negative/boundary: the empty string is neither a concrete tag nor
    a tier name - it returns the default and does not raise."""
    _patch_default(monkeypatch)
    assert opu._resolve_local_model("") == SENTINEL_DEFAULT