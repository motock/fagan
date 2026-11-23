"""Decompose must resolve its roles from the calling team's routing.

``PipelineService.decompose_plan`` runs on behalf of a request that names a
repo (and so a team). Before this fix it passed no ``layer_routing`` to
``_run_decompose_detailed``, so the decompose role fell back to the deployment
registry (claude/opus) and a hosted team that routed every role to ollama hit
the deployment's own, absent, claude credential. These tests pin that the
team's routing reaches the first call AND the JSON-retry call, that the
sizing tier follows the team's dispatch route, and that an empty or failing
routing lookup degrades to the registry default without raising.
"""

import json

import pytest

from pipeline import planner, remote_scope
from pipeline import service as service_module

VALID_PLAN = json.dumps({"epics": []})
TEAM_ROUTING = {
    "decompose": {"provider": "ollama", "model": "gpt-oss"},
    "dispatch": {"provider": "ollama", "model": "gpt-oss"},
}


@pytest.fixture
def clone_root(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    monkeypatch.setattr(remote_scope, "clone_root_for", lambda repo_url: (str(root), None))
    return root


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper
    from pipeline import server as p

    d = tmp_path / "agents"
    d.mkdir()
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


def _stub_routing(monkeypatch, routing):
    monkeypatch.setattr(
        service_module, "layer_routing_for_request", lambda repo_url=None: routing
    )


def _install_backend(monkeypatch, replies):
    """Record every complete() call; replies are returned in order (or raised)."""
    from app import backend

    calls = []
    queue = list(replies)

    class _Driver:
        def __init__(self, name):
            self.name = name

        def complete(self, prompt, *, system, model, allowed_tools):
            calls.append({"name": self.name, "model": model, "system": system})
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(backend, "get_backend", lambda role, *, name: _Driver(name))
    return calls


def test_decompose_plan_uses_calling_team_routing_for_decompose(
    monkeypatch, clone_root, agents_dir
):
    _stub_routing(monkeypatch, TEAM_ROUTING)
    calls = _install_backend(monkeypatch, [VALID_PLAN])

    result = service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    assert result["ok"] is True
    assert calls[0]["name"] == "ollama"
    assert calls[0]["model"] == "gpt-oss:20b"


def test_json_retry_uses_the_same_team_routing(monkeypatch, clone_root, agents_dir):
    _stub_routing(monkeypatch, TEAM_ROUTING)
    calls = _install_backend(monkeypatch, ["not json", VALID_PLAN])

    result = service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    assert result["ok"] is True
    assert len(calls) == 2
    assert [(c["name"], c["model"]) for c in calls] == [("ollama", "gpt-oss:20b"), ("ollama", "gpt-oss:20b")]


def test_sizing_tier_follows_the_team_dispatch_route(monkeypatch, clone_root, agents_dir):
    local_dispatch = {
        "decompose": {"provider": "ollama", "model": "gpt-oss"},
        "dispatch": {"provider": "ollama", "model": "devstral"},
    }
    _stub_routing(monkeypatch, local_dispatch)
    local_calls = _install_backend(monkeypatch, [VALID_PLAN])
    service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    claude_dispatch = {
        "decompose": {"provider": "ollama", "model": "gpt-oss"},
        "dispatch": {"provider": "claude", "model": "opus"},
    }
    _stub_routing(monkeypatch, claude_dispatch)
    claude_calls = _install_backend(monkeypatch, [VALID_PLAN])
    service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    local_system = local_calls[0]["system"]
    claude_system = claude_calls[0]["system"]
    assert planner._STRENGTH_TIER_GUIDANCE["local"] in local_system
    assert planner._STRENGTH_TIER_GUIDANCE["claude"] in claude_system
    assert local_system != claude_system


def test_empty_routing_resolves_the_registry_default(monkeypatch, clone_root, agents_dir):
    _stub_routing(monkeypatch, {})
    calls = _install_backend(monkeypatch, [VALID_PLAN])

    result = service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    assert result["ok"] is True
    assert calls[0]["name"] == _resolved_default_provider()


def _resolved_default_provider():
    from app import role_registry

    return role_registry.resolve_role("decompose", model_fallback=lambda: "opus").provider


def test_routing_lookup_raising_internally_degrades_to_registry_default(
    monkeypatch, clone_root, agents_dir
):
    from pipeline import ci

    def _boom(manifest):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(ci, "context_from_manifest", _boom)
    calls = _install_backend(monkeypatch, [VALID_PLAN])

    result = service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    assert result["ok"] is True
    assert calls[0]["name"] == _resolved_default_provider()


def test_backend_failure_under_team_routing_keeps_detail_text(
    monkeypatch, clone_root, agents_dir
):
    _stub_routing(monkeypatch, TEAM_ROUTING)
    _install_backend(monkeypatch, [RuntimeError("claude CLI call failed: no access")])

    result = service_module.PipelineService().decompose_plan("goal", repo_url="https://example.test/r.git")

    assert result["ok"] is False
    assert "decompose backend returned no output" in result["error"]
    assert "claude CLI call failed" in result["error"]
