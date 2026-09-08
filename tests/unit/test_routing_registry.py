"""The shipped `routing` block in model_registry.json is well-formed and inert.

Membership-only assertions by design: model_registry.json is a cumulative
artifact later stories legitimately extend, so these tests assert presence and
resolvability — never the block's exact tier list or file contents.

Inertness contract: routing.dispatch's default tier must resolve to the same
backend the dispatch role already uses (roles.dispatch: ollama /
gpt-oss-20b-high, whose driver is the same OllamaDriver that the legacy
"local" backend name selects — app.backend._DRIVERS), so a low-risk
software-engineer story routes exactly as it did before the block shipped.
"""

import pytest

from app.role_registry import load_registry, resolve_route

LOW_RISK_STORY = {"risk": "low", "persona": "software-engineer"}


def test_routing_block_declares_dispatch():
    reg = load_registry()
    assert "routing" in reg
    assert "dispatch" in reg["routing"]


def test_dispatch_default_tier_resolves():
    reg = load_registry()
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert resolution is not None


def test_dispatch_default_tier_matches_roles_dispatch():
    """The shipped block is inert: its default tier resolves to the same
    backend the dispatch role already uses. roles.dispatch names the ollama
    provider; the tier names the "local" provider — both are local-transport
    names (pipeline.config._LOCAL_BACKEND_NAMES) backed by the same
    OllamaDriver family, and both select the same concrete model, read from
    the live registry so nothing is hardcoded here."""
    reg = load_registry()
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    roles_dispatch = reg["roles"]["dispatch"]
    # Same concrete model as roles.dispatch, however the registry surfaces it
    # (model key or resolved tag)...
    ollama_entry = reg["providers"][roles_dispatch["provider"]]["models"][
        roles_dispatch["model"]
    ]
    assert resolution.model in {roles_dispatch["model"], ollama_entry.get("tag")}
    # ...served by a local-transport provider (the same driver family the
    # legacy "local" backend name selects), not a hosted one.
    from pipeline.config import _LOCAL_BACKEND_NAMES

    assert resolution.provider in _LOCAL_BACKEND_NAMES


def test_dispatch_low_risk_story_resolves_repeatably():
    """Resolution is a pure read: a second call with the same story must
    return the same tier/provider/model (no state carried between calls)."""
    reg = load_registry()
    first = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    second = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert first == second


def test_routing_rules_only_use_supported_predicates():
    """Every shipped rule's `when` keys are within the two supported
    predicates (max_risk, persona) — anything else makes resolve_route raise,
    so this doubles as a cheap well-formedness guard."""
    reg = load_registry()
    for rule in reg["routing"]["dispatch"].get("rules", []):
        assert set(rule.get("when", {})) <= {"max_risk", "persona"}


@pytest.mark.parametrize(
    "story",
    [
        {"risk": "low", "persona": "software-engineer"},
        {"risk": "high", "persona": "software-engineer"},
    ],
    ids=["low-risk", "high-risk"],
)
def test_resolve_route_dispatch_does_not_raise(story):
    # A malformed shipped block raises RoleRegistryError here; the call site
    # in pipeline.usage._route_dispatch_backend fails open instead, but the
    # shipped file itself must be clean.
    resolve_route("dispatch", story=story)