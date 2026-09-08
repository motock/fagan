"""Unit tests for app.role_registry.resolve_route().

Every test passes a SYNTHETIC ``registry=`` dict — no test reads the live
model_registry.json or asserts against today's configured providers/models
(per .claude/rules/testing-config-gates.md). The hardcoded-fallback
invariant (a registry with no ``routing`` block returns None) is asserted
against a synthetic registry too.
"""
from __future__ import annotations

import pytest

from app.role_registry import RoleRegistryError, RouteResolution, resolve_route

PROVIDERS = {
    "ollama": {"models": {"glm": {"tag": "glm4:9b"}}},
    "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4-5"}}},
}


def _registry(
    rules=None,
    *,
    default_tier="cheap",
    tiers=None,
    role="dispatch",
):
    if tiers is None:
        tiers = {
            "cheap": {"provider": "ollama", "model": "glm"},
            "strong": {"provider": "claude", "model": "sonnet"},
        }
    return {
        "providers": PROVIDERS,
        "routing": {
            role: {
                "tiers": tiers,
                "rules": list(rules) if rules is not None else [],
                "default_tier": default_tier,
            }
        },
    }


# ---------- 1. absent routing config is not an error ----------


def test_registry_without_routing_key_returns_none():
    assert resolve_route("dispatch", registry={"providers": PROVIDERS}) is None


def test_role_missing_from_routing_block_returns_none():
    assert resolve_route("review", registry=_registry()) is None


# ---------- 2. first matching rule -> tier -> concrete tag ----------


def test_matching_rule_returns_tier_provider_and_concrete_tag():
    reg = _registry(rules=[{"when": {"max_risk": "low"}, "tier": "cheap"}])
    res = resolve_route("dispatch", story={"risk": "low"}, registry=reg)
    assert res == RouteResolution(tier="cheap", provider="ollama", model="glm4:9b")
    # The friendly name must never leak through as the resolved model.
    assert res.model != "glm"


# ---------- 3. rule order: first match wins ----------


def test_first_matching_rule_wins_over_later_match():
    reg = _registry(
        rules=[
            {"when": {"max_risk": "low"}, "tier": "cheap"},
            {"when": {"persona": "security-engineer"}, "tier": "strong"},
        ]
    )
    story = {"risk": "low", "persona": "security-engineer"}
    res = resolve_route("dispatch", story=story, registry=reg)
    assert res.tier == "cheap"
    assert res.tier != "strong"


# ---------- 4. default_tier when no rule matches ----------


def test_default_tier_used_when_no_rule_matches():
    reg = _registry(
        rules=[{"when": {"persona": "security-engineer"}, "tier": "strong"}],
    )
    res = resolve_route(
        "dispatch", story={"persona": "backend-dev", "risk": "low"}, registry=reg
    )
    assert res.tier == "cheap"
    assert res.provider == "ollama"
    assert res.model == "glm4:9b"


# ---------- 5. max_risk boundary (inclusive, fail closed) ----------


def test_max_risk_boundary_is_inclusive():
    reg = _registry(
        rules=[{"when": {"max_risk": "low"}, "tier": "cheap"}],
        default_tier="strong",
    )
    assert resolve_route("dispatch", story={"risk": "low"}, registry=reg).tier == "cheap"
    assert (
        resolve_route("dispatch", story={"risk": "medium"}, registry=reg).tier
        == "strong"
    )


def test_missing_or_unknown_risk_is_treated_as_highest():
    reg = _registry(
        rules=[{"when": {"max_risk": "low"}, "tier": "cheap"}],
        default_tier="strong",
    )
    assert resolve_route("dispatch", story={}, registry=reg).tier == "strong"
    assert (
        resolve_route("dispatch", story={"risk": "cosmic"}, registry=reg).tier
        == "strong"
    )


def test_persona_predicate_is_exact_match():
    reg = _registry(
        rules=[{"when": {"persona": "security-engineer"}, "tier": "strong"}],
    )
    matched = resolve_route(
        "dispatch", story={"persona": "security-engineer"}, registry=reg
    )
    assert matched.tier == "strong"
    unmatched = resolve_route(
        "dispatch", story={"persona": "security-engineer-lead"}, registry=reg
    )
    assert unmatched.tier == "cheap"
    no_persona = resolve_route("dispatch", story={"risk": "low"}, registry=reg)
    assert no_persona.tier == "cheap"


# ---------- plan-level routing beats the registry's own ----------


def test_plan_level_routing_beats_registry_routing():
    reg = _registry(default_tier="cheap")
    plan = {
        "routing": {
            "dispatch": {
                "tiers": {"strong": {"provider": "claude", "model": "sonnet"}},
                "rules": [],
                "default_tier": "strong",
            }
        }
    }
    res = resolve_route(
        "dispatch", story={"risk": "low"}, registry=reg, plan_role_config=plan
    )
    assert res.tier == "strong"
    assert res.provider == "claude"
    assert res.model == "claude-sonnet-4-5"


# ---------- 6. fail closed on malformed routing blocks ----------


def test_rule_naming_unknown_tier_raises_naming_it():
    reg = _registry(rules=[{"when": {"max_risk": "low"}, "tier": "turbo"}])
    with pytest.raises(RoleRegistryError, match="turbo"):
        resolve_route("dispatch", story={"risk": "low"}, registry=reg)


def test_tier_naming_unknown_provider_raises_naming_it():
    reg = _registry(tiers={"cheap": {"provider": "together", "model": "glm"}})
    with pytest.raises(RoleRegistryError, match="together"):
        resolve_route("dispatch", registry=reg)


def test_tier_naming_unknown_model_for_provider_raises_naming_it():
    reg = _registry(tiers={"cheap": {"provider": "ollama", "model": "gpt5"}})
    with pytest.raises(RoleRegistryError, match="gpt5"):
        resolve_route("dispatch", registry=reg)


def test_unrecognized_when_predicate_raises_naming_the_key():
    reg = _registry(rules=[{"when": {"min_risk": "low"}, "tier": "cheap"}])
    with pytest.raises(RoleRegistryError, match="min_risk"):
        resolve_route("dispatch", story={"risk": "low"}, registry=reg)


def test_bad_predicate_in_later_rule_raises_even_after_a_match():
    reg = _registry(
        rules=[
            {"when": {"max_risk": "low"}, "tier": "cheap"},
            {"when": {"bogus": "x"}, "tier": "strong"},
        ]
    )
    with pytest.raises(RoleRegistryError, match="bogus"):
        resolve_route("dispatch", story={"risk": "low"}, registry=reg)


def test_unknown_default_tier_raises_naming_it():
    reg = _registry(default_tier="turbo")
    with pytest.raises(RoleRegistryError, match="turbo"):
        resolve_route("dispatch", registry=reg)