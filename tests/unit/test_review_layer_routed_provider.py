"""_run_reviewer must send the review to the provider a layer's role_routing
named, not only to a provider named by a plan's role_config or the registry.

Before this fix a provider that came from layer_routing left get_backend's
name as None, so get_backend fell back to PIPELINE_BACKEND_REVIEW / claude
and the Claude CLI was handed an Ollama model tag.
"""
import json

import pytest

from app import role_registry
from pipeline import persona as pper
from pipeline import review


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    # An empty registry (no roles.review) and no review backend env var, so
    # only the layer/plan sources under test can name a provider.
    registry = tmp_path / "model_registry.json"
    registry.write_text(json.dumps({
        "providers": {"claude": {"models": {"haiku": {"tag": "haiku"}}}},
        "roles": {},
    }))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(registry))
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    role_registry.reset_registry_cache()
    yield
    role_registry.reset_registry_cache()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


class _RecordingDriver:
    def __init__(self):
        self.model = None

    def complete(self, prompt, **kwargs):
        self.model = kwargs.get("model")
        return "VERDICT: APPROVE"


@pytest.fixture
def get_backend_calls(monkeypatch):
    calls = []
    driver = _RecordingDriver()

    def _fake_get_backend(role, name=None):
        calls.append({"role": role, "name": name})
        return driver

    monkeypatch.setattr(review.backend, "get_backend", _fake_get_backend)
    return calls, driver


def _run(tmp_path, **kwargs):
    # A path that is not a git worktree keeps the diff pre-materialization out
    # of the way; only the driver/model selection is under test.
    return review._run_reviewer(str(tmp_path / "no-worktree"), "agent/story-1", **kwargs)


def test_layer_routed_ollama_provider_selects_ollama_driver(
    agents_dir, get_backend_calls, tmp_path,
):
    calls, driver = get_backend_calls
    _run(tmp_path, layer_routing={"review": {"provider": "ollama", "model": "glm-5.3-flash:cloud"}})
    assert calls[0]["name"] == "ollama"
    assert driver.model == "glm-5.3-flash:cloud"


def test_layer_routed_claude_provider_selects_claude_driver(
    agents_dir, get_backend_calls, tmp_path,
):
    calls, _ = get_backend_calls
    _run(tmp_path, layer_routing={"review": {"provider": "claude", "model": "haiku"}})
    assert calls[0]["name"] == "claude"


def test_no_routing_anywhere_passes_none_as_driver_name(
    agents_dir, get_backend_calls, tmp_path,
):
    calls, _ = get_backend_calls
    _run(tmp_path)
    assert calls[0]["name"] is None


def test_explicit_backend_name_wins_over_layer_routed_provider(
    agents_dir, get_backend_calls, tmp_path,
):
    calls, driver = get_backend_calls
    _run(
        tmp_path,
        backend_name="claude",
        layer_routing={"review": {"provider": "ollama", "model": "glm-5.3-flash:cloud"}},
    )
    assert calls[0]["name"] == "claude"
    assert driver.model != "glm-5.3-flash:cloud"


def test_plan_role_config_provider_wins_over_layer_routing(
    agents_dir, get_backend_calls, tmp_path,
):
    calls, _ = get_backend_calls
    _run(
        tmp_path,
        plan_role_config={"review": {"provider": "claude"}},
        layer_routing={"review": {"provider": "ollama", "model": "glm-5.3-flash:cloud"}},
    )
    assert calls[0]["name"] == "claude"


@pytest.mark.parametrize("layer_routing", [
    {"review": {"provider": ""}},
    None,
])
def test_malformed_layer_routing_leaves_driver_name_unset(
    agents_dir, get_backend_calls, tmp_path, layer_routing,
):
    calls, _ = get_backend_calls
    _run(tmp_path, layer_routing=layer_routing)
    assert calls[0]["name"] is None
