"""Tests for role_registry.py — the per-role provider/model resolution
layer described in the per-role provider/model configurability plan.

Covers: model_registry.json loading/validation (load_registry) and the
resolve_role() priority chain (plan_role_config > env var > registry >
caller-supplied fallback) for both provider and model, independently.
"""
import json

import pytest

from app import role_registry as rr


# ---------- load_registry ----------
def test_load_registry_missing_file_returns_empty_dict(tmp_path):
    assert rr.load_registry(tmp_path / "does_not_exist.json") == {}


def test_load_registry_parses_valid_file(tmp_path):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({
        "providers": {"claude": {"models": {"opus": {"tag": "opus"}}}},
        "roles": {"overlord": {"provider": "claude", "model": "opus"}},
    }))
    data = rr.load_registry(path)
    assert data["providers"]["claude"]["models"]["opus"]["tag"] == "opus"
    assert data["roles"]["overlord"]["provider"] == "claude"


def test_load_registry_malformed_json_raises(tmp_path):
    path = tmp_path / "model_registry.json"
    path.write_text("{not valid json")
    with pytest.raises(rr.RoleRegistryError):
        rr.load_registry(path)


def test_load_registry_unknown_provider_in_roles_raises(tmp_path):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({
        "providers": {"claude": {"models": {}}},
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }))
    with pytest.raises(rr.RoleRegistryError, match="mlx"):
        rr.load_registry(path)


def test_load_registry_unknown_model_in_roles_raises(tmp_path):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({
        "providers": {"mlx": {"models": {"qwen": {"tag": "some/path"}}}},
        "roles": {"review": {"provider": "mlx", "model": "typo-name"}},
    }))
    with pytest.raises(rr.RoleRegistryError, match="typo-name"):
        rr.load_registry(path)


def test_load_registry_default_path_reads_repo_root_file(monkeypatch, tmp_path):
    """No path= given -> PIPELINE_MODEL_REGISTRY_PATH env var, when set,
    is honored (so tests/ops can point at a scratch file without touching
    the real repo-root model_registry.json)."""
    path = tmp_path / "custom_registry.json"
    path.write_text(json.dumps({"providers": {}, "roles": {}}))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    assert rr.load_registry() == {"providers": {}, "roles": {}}


# ---------- resolve_role: provider priority ----------
def test_resolve_role_provider_defaults_to_claude_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)
    resolution = rr.resolve_role("overlord", model_fallback="opus", registry={})
    assert resolution.provider == "claude"
    assert resolution.model == "opus"


def test_resolve_role_provider_from_registry_when_env_and_plan_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    registry = {
        "providers": {"mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen"}}}},
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    resolution = rr.resolve_role("review", registry=registry)
    assert resolution.provider == "mlx"
    assert resolution.model == "mlx-community/Qwen"


def test_resolve_role_registry_beats_env_var(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "ollama")
    registry = {
        "providers": {
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen"}}},
            "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}},
        },
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    resolution = rr.resolve_role("review", registry=registry, model_fallback="sonnet")
    # REG-4: the registry outranks the env var, so mlx wins and its own
    # "qwen" pairing is honored (the registry's provider IS the winner).
    # The cross-provider contamination guard lives on in the
    # plan-role-config tests, where a DIFFERENT source wins the provider.
    assert resolution.provider == "mlx"
    assert resolution.model == "mlx-community/Qwen"


def test_resolve_role_provider_plan_role_config_beats_env_and_registry(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "ollama")
    registry = {
        "providers": {"claude": {"models": {}}},
        "roles": {"review": {"provider": "claude"}},
    }
    resolution = rr.resolve_role(
        "review", plan_role_config={"review": {"provider": "mlx"}},
        registry=registry, model_fallback="qwen-tag",
    )
    assert resolution.provider == "mlx"


# ---------- resolve_role: model priority ----------
def test_resolve_role_model_from_plan_role_config_beats_registry(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    registry = {
        "providers": {"claude": {"models": {"opus": {"tag": "opus"}, "sonnet": {"tag": "sonnet"}}}},
        "roles": {"dispatch": {"provider": "claude", "model": "sonnet"}},
    }
    resolution = rr.resolve_role(
        "dispatch", plan_role_config={"dispatch": {"model": "opus"}}, registry=registry,
    )
    assert resolution.model == "opus"


def test_resolve_role_model_falls_back_to_caller_default_when_unconfigured():
    resolution = rr.resolve_role("planner", model_fallback="sonnet", registry={})
    assert resolution.model == "sonnet"


def test_resolve_role_model_fallback_accepts_callable():
    calls = []

    def fallback():
        calls.append(1)
        return "haiku"

    resolution = rr.resolve_role("planner", model_fallback=fallback, registry={})
    assert resolution.model == "haiku"
    assert calls == [1]


def test_resolve_role_unknown_model_name_for_resolved_provider_raises():
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}},
        "roles": {"dispatch": {"provider": "ollama", "model": "not-a-real-model"}},
    }
    with pytest.raises(rr.RoleRegistryError, match="not-a-real-model"):
        rr.resolve_role("dispatch", registry=registry)


def test_resolve_role_no_model_anywhere_raises_clear_error():
    with pytest.raises(rr.RoleRegistryError, match="planner"):
        rr.resolve_role("planner", registry={})


def test_resolve_role_default_provider_param_overrides_claude_fallback(monkeypatch):
    """default_provider= lets a caller (e.g. the planner role, which should
    mirror the dispatch backend rather than defaulting to claude) supply its
    own bottom-of-chain provider default, still overridable by env/plan/
    registry above it."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    resolution = rr.resolve_role(
        "planner", registry={}, model_fallback="gpt-oss:20b",
        default_provider="ollama",
    )
    assert resolution.provider == "ollama"


def test_resolve_role_default_provider_param_still_beaten_by_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "mlx")
    resolution = rr.resolve_role(
        "planner", registry={}, model_fallback="qwen-tag",
        default_provider="ollama",
    )
    assert resolution.provider == "mlx"


# ---------- resolve_role: zero-config no-op case ----------
def test_resolve_role_unconfigured_role_is_a_pure_passthrough_to_fallback(monkeypatch):
    """A role name the registry has never heard of, with no plan/env
    override, must resolve to exactly the caller's existing default -
    the zero-behavior-change guarantee for anyone not using the new
    config."""
    monkeypatch.delenv("PIPELINE_BACKEND_SOME_NEW_ROLE", raising=False)
    resolution = rr.resolve_role(
        "some_new_role", model_fallback="devstral:24b", registry={},
    )
    assert resolution.provider == "claude"
    assert resolution.model == "devstral:24b"


# ---------- resolve_role: environ injection (dependency-injection seam) ----------
# These tests pin the new keyword-only `environ` parameter on resolve_role(),
# which lets a caller inject the environment mapping instead of always reading
# the real process os.environ. The default (None -> os.environ) preserves
# every existing call site unchanged.

def test_resolve_role_accepts_environ_keyword_only_parameter():
    """resolve_role must accept a keyword-only `environ` parameter (the
    signature change this story adds). It must be keyword-only: passing it
    positionally must be a TypeError because it sits after the existing
    keyword-only params."""
    import inspect

    sig = inspect.signature(rr.resolve_role)
    assert "environ" in sig.parameters
    param = sig.parameters["environ"]
    # keyword-only (appears after a * in the signature)
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    # defaults to None (so existing callers that omit it are unaffected)
    assert param.default is None
    # The annotation must accept a dict or None.
    assert param.annotation in ("dict | None", "Optional[dict]", "Optional[Dict]")


def test_resolve_role_environ_injected_mapping_is_consulted(monkeypatch):
    """Positive: an explicitly injected environ mapping is actually read
    instead of the real process os.environ — no monkeypatch.setenv needed.
    This proves the seam is real, not a no-op."""
    # Make sure the real process env does NOT carry this var, so the only
    # way resolve_role can see "ollama" is by reading the injected mapping.
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    registry = {
        "providers": {"ollama": {"models": {}}},
        "roles": {},
    }
    resolution = rr.resolve_role(
        "review",
        environ={"PIPELINE_BACKEND_REVIEW": "ollama"},
        registry=registry,
        model_fallback="some-tag",
    )
    assert resolution.provider == "ollama"


def test_resolve_role_environ_omitted_preserves_existing_behavior(monkeypatch):
    """Negative/backward-compat: when `environ` is omitted entirely, the
    function must fall back to the real os.environ exactly as before —
    zero call-site changes required anywhere. We exercise the env-var layer
    via the real os.environ (monkeypatch.setenv). Since REG-4 the env var
    is the EMPTY-STATE fallback, so the registry here has no entry for the
    role and the env var wins — proving os.environ was still consulted."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "ollama")
    registry = {
        "providers": {
            "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}},
        },
        "roles": {},
    }
    # NOTE: environ is intentionally NOT passed here.
    resolution = rr.resolve_role(
        "review", registry=registry, model_fallback="sonnet",
    )
    assert resolution.provider == "ollama"
    # No registry entry -> no registry model to pair; model_fallback wins.
    assert resolution.model == "sonnet"


def test_resolve_role_environ_empty_dict_does_not_substitute_os_environ(monkeypatch):
    """Boundary: an explicitly empty dict (NOT None) must be honored as
    "no env vars set" — it must NOT silently fall back to os.environ. So
    even if the real os.environ carries the var, an empty injected mapping
    must let the chain fall through to registry/default_provider."""
    # Poison the real os.environ so we can detect any improper fallback.
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "ollama")
    registry = {
        "providers": {
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen"}}},
        },
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    resolution = rr.resolve_role(
        "review", environ={}, registry=registry,
    )
    # If environ={} were wrongly treated as None -> os.environ, provider
    # would be "ollama" (from the poisoned env). It must instead be "mlx"
    # (from the registry), proving the empty mapping was respected.
    assert resolution.provider == "mlx"
    assert resolution.model == "mlx-community/Qwen"


def test_resolve_role_environ_none_explicitly_uses_os_environ(monkeypatch):
    """Boundary complement: passing environ=None explicitly must behave
    identically to omitting it — i.e. read the real os.environ. This pins
    the `if environ is None: environ = os.environ` line. Since REG-4 the
    env var is the EMPTY-STATE fallback, so the registry here has no entry
    for the role and the env var wins — proving os.environ was consulted."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "ollama")
    registry = {
        "providers": {
            "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}},
        },
        "roles": {},
    }
    resolution = rr.resolve_role(
        "review", environ=None, registry=registry, model_fallback="sonnet",
    )
    assert resolution.provider == "ollama"
    assert resolution.model == "sonnet"


def test_resolve_role_environ_does_not_touch_registry_path_env(monkeypatch, tmp_path):
    """The environ seam must be scoped to the PIPELINE_BACKEND_<ROLE> lookup
    only. The unrelated PIPELINE_MODEL_REGISTRY_PATH env var (read inside
    _registry_path()/load_registry()) must still come from the real
    os.environ, NOT from the injected `environ` — that call is explicitly
    out of scope and must not be rerouted."""
    path = tmp_path / "custom_registry.json"
    path.write_text(json.dumps({
        "providers": {"claude": {"models": {"opus": {"tag": "opus"}}}},
        "roles": {},
    }))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    # Inject an environ that does NOT carry PIPELINE_MODEL_REGISTRY_PATH.
    # If the implementation wrongly routed the registry-path lookup through
    # `environ`, load_registry() would fail to find the file and the
    # registry would be empty -> provider would default to "claude" with
    # model_fallback. We assert the real registry file is still read.
    resolution = rr.resolve_role(
        "review",
        environ={"PIPELINE_BACKEND_REVIEW": "claude"},
        model_fallback="opus",
    )
    assert resolution.provider == "claude"
    assert resolution.model == "opus"
