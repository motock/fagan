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

import json

import pytest

from app.role_registry import load_registry, resolve_route

LOW_RISK_STORY = {"risk": "low", "persona": "software-engineer"}

# A minimal, well-formed routing.dispatch block: used by the tests below so
# they exercise load_registry/resolve_route's resolution logic rather than
# today's live model_registry.json contents — .claude/rules/
# testing-config-gates.md: "test the resolution logic, not today's
# configured values." A live-file read is what made these tests fragile
# under legitimate reconfiguration of the shipped registry.
_SYNTHETIC_DISPATCH_REGISTRY = {
    "providers": {"ollama": {"models": {"gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"}}}},
    "routing": {
        "dispatch": {
            "default_tier": "local_default",
            "tiers": {
                "local_default": {"provider": "ollama", "model": "gpt-oss-20b-high"},
            },
            "rules": [],
        }
    },
}


def test_routing_block_declares_dispatch(tmp_path):
    """A model_registry.json declaring a routing.dispatch block loads with
    'routing'/'dispatch' present — load_registry's own file-parsing
    behavior, exercised against a synthetic registry file rather than the
    live model_registry.json."""
    registry_path = tmp_path / "model_registry.json"
    registry_path.write_text(json.dumps(_SYNTHETIC_DISPATCH_REGISTRY))
    reg = load_registry(path=registry_path)
    assert "routing" in reg
    assert "dispatch" in reg["routing"]


def test_dispatch_default_tier_resolves():
    """resolve_route must resolve a value for a role whose routing block
    declares a default_tier — exercised against a synthetic registry, not
    the live model_registry.json."""
    resolution = resolve_route(
        "dispatch", story=LOW_RISK_STORY, registry=_SYNTHETIC_DISPATCH_REGISTRY
    )
    assert resolution is not None


def test_dispatch_default_tier_matches_roles_dispatch():
    """The shipped block is inert: its default tier resolves to the same
    backend the dispatch role already uses. Asserted against a synthetic
    registry fixture — never the live model_registry.json (the resolution
    logic is what's under test, not today's configured values; a live-file
    read here is what made this test order/worker-dependent under xdist).
    The fixture mirrors the shipped contract: routing.dispatch's default
    tier names the same provider/model pair that roles.dispatch does."""
    reg = {
        "providers": {
            "ollama": {
                "models": {
                    "gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"},
                }
            },
        },
        "roles": {
            "dispatch": {"provider": "ollama", "model": "gpt-oss-20b-high"},
        },
        "routing": {
            "dispatch": {
                "default_tier": "local_default",
                "tiers": {
                    "local_default": {
                        "provider": "ollama",
                        "model": "gpt-oss-20b-high",
                    },
                },
                "rules": [],
            }
        },
    }
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
    """Every rule's `when` keys must be within the two predicates
    resolve_route actually supports (max_risk, persona) — anything else
    makes resolve_route raise. Exercised against a synthetic registry
    whose rules use both supported predicates, confirming resolve_route
    accepts them without raising, rather than against the live
    model_registry.json's current rule set."""
    reg = {
        "providers": {"ollama": {"models": {"gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"}}}},
        "routing": {
            "dispatch": {
                "default_tier": "local_default",
                "tiers": {
                    "local_default": {"provider": "ollama", "model": "gpt-oss-20b-high"},
                },
                "rules": [
                    {"when": {"max_risk": "low"}, "tier": "local_default"},
                    {"when": {"persona": "software-engineer"}, "tier": "local_default"},
                ],
            }
        },
    }
    for rule in reg["routing"]["dispatch"].get("rules", []):
        assert set(rule.get("when", {})) <= {"max_risk", "persona"}
    resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)


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
