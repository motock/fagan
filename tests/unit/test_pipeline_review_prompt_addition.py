import json

from app import pipeline_mcp_server as p


def test_run_reviewer_includes_new_check_and_preserves_existing(monkeypatch):
    captured = {}
    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"
    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    prompt = captured["prompt"]
    # Check new check present
    assert "(4) if the change adds a module" in prompt
    # Ensure existing numbered checks remain
    for num in ["(1)", "(2)", "(3)"]:
        assert num in prompt


def _write_ollama_glm_review_registry(tmp_path):
    """Mirrors this repo's real model_registry.json shape: roles.review
    pairs provider=ollama with model=glm (tag glm-5.2:cloud)."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps({
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "sonnet"}}},
            "ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}},
        },
        "roles": {"review": {"provider": "ollama", "model": "glm"}},
    }))
    return registry_path


def test_run_reviewer_backend_override_does_not_leak_other_providers_model_tag(
    monkeypatch, tmp_path,
):
    """Escalated review (review_story forcing backend_name="claude" once a
    story has story["escalated"]=True) must not pass through the ollama/glm
    registry pairing's raw model tag ("glm-5.2:cloud") as --model to the
    claude backend - that tag only exists on the ollama provider and the
    claude CLI rejects it outright, permanently breaking escalated review."""
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(_write_ollama_glm_review_registry(tmp_path)))
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["model"] = kwargs.get("model")
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", backend_name="claude")
    assert captured["model"] != "glm-5.2:cloud"
    assert captured["model"] == "sonnet"


def test_run_reviewer_backend_matching_registry_provider_keeps_registry_model(
    monkeypatch, tmp_path,
):
    """Non-escalated path (backend_name unset, matching resolution.provider)
    must be unaffected by the override fallback - the registry-paired model
    tag still flows straight through, exactly as before this fix."""
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(_write_ollama_glm_review_registry(tmp_path)))
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["model"] = kwargs.get("model")
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    assert captured["model"] == "glm-5.2:cloud"
