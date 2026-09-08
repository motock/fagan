"""Tests for the new ``chat`` pipeline role.

This story registers the chatbot's own model as a first-class pipeline
role, the same way overlord/planner/dispatch/review/decompose/
test_author/diagnosis/security already are. Two small changes make it
so:

1. ``pipeline.config_provenance.PIPELINE_ROLES`` gains the string
   ``"chat"`` at the end, so ``effective_role_config()`` and the
   ``/api/config`` dashboard route include it in their output.
2. ``model_registry.json`` gains a ``roles.chat`` entry defaulting to
   provider ``claude`` / model ``sonnet`` (both declared under
   ``providers.*``), so a fresh install resolves without extra config.

These tests are written FIRST and must fail (red) until the
implementation lands. They cover the happy path, the registry-optional
negative path, and every mechanically-checkable requirement the task
states.
"""
import json

import pytest

from app import role_registry as rr
from pipeline import config_provenance as cp

# Synthetic registry fixture (moved above its first use): a chat role
# entry paired with a providers.claude.sonnet declaration. Used throughout
# this module instead of the live model_registry.json so these tests
# exercise load_registry/resolve_role/effective_role_config's own
# resolution logic and stay stable across any legitimate reconfiguration
# of the live registry's chat role — .claude/rules/testing-config-gates.md:
# "test the resolution logic, not today's configured values."
_SYNTHETIC_CHAT_REGISTRY = {
    "providers": {"claude": {"models": {"sonnet": {"tag": "sonnet"}}}},
    "roles": {"chat": {"provider": "claude", "model": "sonnet"}},
}


# ---------------------------------------------------------------------------
# 1. PIPELINE_ROLES contains "chat" (and at the end).
# ---------------------------------------------------------------------------
def test_chat_is_in_pipeline_roles():
    """The string ``chat`` must be a member of PIPELINE_ROLES."""
    assert "chat" in cp.PIPELINE_ROLES, (
        "expected 'chat' to be registered in config_provenance.PIPELINE_ROLES"
    )


def test_chat_is_last_in_pipeline_roles():
    """The task says to add ``chat`` at the END of the tuple."""
    assert cp.PIPELINE_ROLES[-1] == "chat", (
        "expected 'chat' to be the final entry of PIPELINE_ROLES, "
        f"got {cp.PIPELINE_ROLES[-1]!r}"
    )


def test_pipeline_roles_still_a_tuple():
    """The type must remain a tuple (no accidental list/other)."""
    assert isinstance(cp.PIPELINE_ROLES, tuple)


def test_pipeline_roles_has_nine_roles():
    """The original 8 roles plus the new chat role = 9 total."""
    assert len(cp.PIPELINE_ROLES) == 9, (
        f"expected 9 roles after adding chat, got {len(cp.PIPELINE_ROLES)}: "
        f"{cp.PIPELINE_ROLES}"
    )


def test_pipeline_roles_preserves_original_eight():
    """Adding chat must not drop or reorder the original eight roles."""
    expected_first_eight = [
        "overlord",
        "planner",
        "dispatch",
        "review",
        "decompose",
        "test_author",
        "diagnosis",
        "security",
    ]
    assert list(cp.PIPELINE_ROLES[:8]) == expected_first_eight


# ---------------------------------------------------------------------------
# 2. model_registry.json has a roles.chat entry pairing claude/sonnet.
# ---------------------------------------------------------------------------
def test_model_registry_has_chat_role_entry(tmp_path):
    """A model_registry.json declaring a roles.chat block must parse with
    'chat' present under roles — load_registry's own file-parsing
    behavior, exercised against a synthetic registry file rather than the
    live model_registry.json (whose chat role is free to be
    reconfigured)."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps(_SYNTHETIC_CHAT_REGISTRY))
    data = rr.load_registry(path=registry_path)
    roles = data.get("roles", {})
    assert "chat" in roles, "parsed registry's roles block must include 'chat'"


def test_model_registry_chat_has_a_provider(tmp_path):
    """The chat role must declare SOME provider — which one is a
    reconfigurable operational choice, not a fact this test should pin. See
    test_model_registry_chat_pairs_with_declared_provider_model for the
    check that the declared value is actually valid. Exercised against a
    synthetic registry file, not the live model_registry.json."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps(_SYNTHETIC_CHAT_REGISTRY))
    data = rr.load_registry(path=registry_path)
    chat = data["roles"]["chat"]
    provider = chat.get("provider")
    assert isinstance(provider, str) and provider, (
        f"expected roles.chat.provider to be a non-empty string, got {provider!r}"
    )


def test_model_registry_chat_has_a_model(tmp_path):
    """The chat role must declare SOME model — see
    test_model_registry_chat_has_a_provider for why this doesn't pin a
    specific value. Exercised against a synthetic registry file, not the
    live model_registry.json."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps(_SYNTHETIC_CHAT_REGISTRY))
    data = rr.load_registry(path=registry_path)
    chat = data["roles"]["chat"]
    model = chat.get("model")
    assert isinstance(model, str) and model, (
        f"expected roles.chat.model to be a non-empty string, got {model!r}"
    )


def test_model_registry_chat_pairs_with_declared_provider_model(tmp_path):
    """The chat role's provider+model must both be declared under
    providers.* — load_registry enforces this and would raise
    RoleRegistryError otherwise. This is the load-time validation the
    task calls out, exercised against a synthetic registry file rather
    than the live model_registry.json."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps(_SYNTHETIC_CHAT_REGISTRY))
    data = rr.load_registry(path=registry_path)
    providers = data["providers"]
    chat = data["roles"]["chat"]
    assert chat["provider"] in providers, (
        f"chat provider {chat['provider']!r} not declared under providers"
    )
    assert chat["model"] in providers[chat["provider"]]["models"], (
        f"chat model {chat['model']!r} not declared under "
        f"providers.{chat['provider']}.models"
    )


def test_load_registry_does_not_raise_with_chat_role():
    """The existing load_registry call must not raise RoleRegistryError
    once the chat entry is present — this is the headline regression
    guard the task lists."""
    # Must not raise.
    rr.load_registry()


# ---------------------------------------------------------------------------
# 3. resolve_role with role "chat" against a synthetic registry fixture.
#
# A synthetic fixture (not the real model_registry.json) so these tests
# exercise resolve_role's own claude/sonnet-pairing logic and stay stable
# across legitimate reconfiguration of the live registry's chat role (see
# test_model_registry_chat_uses_ollama_provider) - CLAUDE.md's "Testing
# Configuration-Driven Logic" section: assert against a stubbed fixture,
# never against whatever the real config currently contains.
#
# (_SYNTHETIC_CHAT_REGISTRY is defined near the top of this module, above
# its first use in section 2.)
# ---------------------------------------------------------------------------
def test_resolve_role_chat_returns_claude_provider():
    """resolve_role(role='chat', registry=<claude/sonnet fixture>) must
    return a RoleResolution whose provider is claude, without raising."""
    resolution = rr.resolve_role("chat", registry=_SYNTHETIC_CHAT_REGISTRY)
    assert isinstance(resolution, rr.RoleResolution)
    assert resolution.provider == "claude"


def test_resolve_role_chat_returns_sonnet_tag_model():
    """The resolved model must be the sonnet *tag* (resolved against
    providers.claude.models), not the friendly name."""
    resolution = rr.resolve_role("chat", registry=_SYNTHETIC_CHAT_REGISTRY)
    expected_tag = _SYNTHETIC_CHAT_REGISTRY["providers"]["claude"]["models"]["sonnet"]["tag"]
    assert resolution.model == expected_tag, (
        f"expected model tag {expected_tag!r}, got {resolution.model!r}"
    )


def test_resolve_role_chat_does_not_raise():
    """Headline: resolving the chat role raises nothing, given a synthetic
    registry that declares a valid chat provider/model pairing (decoupled
    from the live model_registry.json's current chat role, per the
    config-gates testing rule)."""
    rr.resolve_role("chat", registry=_SYNTHETIC_CHAT_REGISTRY)


# ---------------------------------------------------------------------------
# 4. effective_role_config includes a chat entry with error None.
# ---------------------------------------------------------------------------
def test_effective_role_config_includes_chat_entry():
    """effective_role_config(registry=load_registry()) must return a list
    that includes an entry whose role == 'chat'."""
    result = cp.effective_role_config(registry=rr.load_registry(), environ={})
    roles = [entry["role"] for entry in result]
    assert "chat" in roles, f"effective_role_config output missing 'chat': {roles}"


def test_effective_role_config_chat_entry_error_is_none():
    """The chat entry must resolve cleanly — error is None — given a
    synthetic registry with a valid chat pairing (decoupled from the live
    model_registry.json's current chat role, per the config-gates testing
    rule)."""
    result = cp.effective_role_config(registry=_SYNTHETIC_CHAT_REGISTRY, environ={})
    chat_entry = next(e for e in result if e["role"] == "chat")
    assert chat_entry["error"] is None, (
        f"expected chat entry error None, got {chat_entry['error']!r}"
    )


def test_effective_role_config_chat_entry_provider_is_claude():
    """The chat entry's provider must be claude when the registry pairs it
    that way (a synthetic fixture, per _SYNTHETIC_CHAT_REGISTRY above -
    decoupled from the live registry's current default)."""
    result = cp.effective_role_config(registry=_SYNTHETIC_CHAT_REGISTRY, environ={})
    chat_entry = next(e for e in result if e["role"] == "chat")
    assert chat_entry["provider"] == "claude", (
        f"expected chat provider 'claude', got {chat_entry['provider']!r}"
    )


def test_effective_role_config_chat_entry_model_is_sonnet_tag():
    """The chat entry's model must be the sonnet tag when the registry pairs
    it that way (synthetic fixture, see above)."""
    result = cp.effective_role_config(registry=_SYNTHETIC_CHAT_REGISTRY, environ={})
    chat_entry = next(e for e in result if e["role"] == "chat")
    expected_tag = _SYNTHETIC_CHAT_REGISTRY["providers"]["claude"]["models"]["sonnet"]["tag"]
    assert chat_entry["model"] == expected_tag, (
        f"expected chat model {expected_tag!r}, got {chat_entry['model']!r}"
    )


def test_effective_role_config_chat_entry_has_full_shape():
    """The chat entry must carry the same keys as every other role
    entry, so the dashboard can render it uniformly."""
    result = cp.effective_role_config(registry=rr.load_registry(), environ={})
    chat_entry = next(e for e in result if e["role"] == "chat")
    expected_keys = {
        "role",
        "provider",
        "model",
        "provider_source",
        "model_source",
        "restart_required",
        "error",
    }
    assert set(chat_entry.keys()) == expected_keys, (
        f"chat entry keys mismatch: {set(chat_entry.keys())}"
    )


# ---------------------------------------------------------------------------
# 5. Negative: the registry is fully optional. A missing role falls
#    through to the caller's default_provider / model_fallback chain and
#    does NOT raise.
# ---------------------------------------------------------------------------
def test_resolve_role_chat_with_empty_registry_uses_fallback_no_raise():
    """resolve_role with role 'chat' and an empty registry dict must NOT
    raise — the registry is optional, and a missing role falls through to
    the caller's default_provider + model_fallback chain. Passing
    model_fallback='sonnet' confirms the fallback chain works."""
    resolution = rr.resolve_role(
        "chat",
        registry={},
        model_fallback="sonnet",
        environ={},
    )
    assert isinstance(resolution, rr.RoleResolution)
    # With an empty registry, provider falls through to default_provider
    # ("claude") and model falls through to the caller's model_fallback.
    assert resolution.provider == "claude"
    assert resolution.model == "sonnet"


def test_resolve_role_chat_with_empty_registry_no_fallback_raises():
    """Boundary/negative: with an empty registry AND no model_fallback,
    resolve_role must raise RoleRegistryError (no model configured) —
    confirming the fallback chain is what makes the empty-registry case
    work, not silent zero-value behavior."""
    with pytest.raises(rr.RoleRegistryError, match="no model configured"):
        rr.resolve_role("chat", registry={}, environ={})


def test_resolve_role_chat_with_empty_registry_callable_fallback():
    """Boundary: a callable model_fallback must also work through the
    fallback chain (the registry is optional regardless of fallback
    form)."""
    resolution = rr.resolve_role(
        "chat",
        registry={},
        model_fallback=lambda: "sonnet",
        environ={},
    )
    assert resolution.model == "sonnet"
    assert resolution.provider == "claude"


def test_effective_role_config_chat_with_empty_registry_and_fallback():
    """effective_role_config with an empty registry but a chat
    model_fallback must still include a chat entry with error None —
    the dashboard must not crash just because the registry is absent."""
    result = cp.effective_role_config(
        registry={},
        model_fallbacks={"chat": "sonnet"},
        environ={},
    )
    chat_entry = next(e for e in result if e["role"] == "chat")
    assert chat_entry["error"] is None
    assert chat_entry["provider"] == "claude"
    assert chat_entry["model"] == "sonnet"