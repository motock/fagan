"""Routing-policy branch in _route_dispatch_backend (pipeline/usage.py).

With PIPELINE_BACKEND_DISPATCH=auto, the a-priori router must first consult
role_registry.resolve_route("dispatch", story=story): a configured routing
policy names the backend (including backends the old two-way local/claude
branch could never return, e.g. litellm). When no policy resolves — or the
registry raises on a bad routing block — the call site fails OPEN to the
pre-policy behavior, exactly as before this story.
"""

from app.role_registry import RoleRegistryError, RouteResolution
from pipeline import dispatch, usage


def _make_fake(state):
    """Switchable resolve_route stand-in driven by a mutable state dict."""

    def fake_resolve_route(role, story=None, **kwargs):
        state["calls"].append((role, dict(story or {})))
        if state["error"] is not None:
            raise state["error"]
        return state["resolution"]

    return fake_resolve_route


def _patch_resolve_route(monkeypatch, state):
    monkeypatch.setattr(
        usage.role_registry, "resolve_route", _make_fake(state)
    )


def test_policy_can_name_third_backend(monkeypatch):
    """Criterion 1: the policy can select a backend the old two-way branch
    (local/claude only) could never return."""
    # Explicit precondition: the policy branch only runs when the operator
    # has not set a PIPELINE_LOCAL_MAX_RISK runtime ceiling.
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_RISK", raising=False)
    state = {"error": None, "resolution": None, "calls": []}
    state["resolution"] = RouteResolution(
        tier="strong", provider="litellm", model="openai/gpt-5-mini"
    )
    _patch_resolve_route(monkeypatch, state)

    assert (
        usage._route_dispatch_backend({"risk": "low", "persona": "software-engineer"})
        == "litellm"
    )


def test_policy_overrides_risk_default(monkeypatch):
    """Criterion 2: a policy selecting claude for a low-risk story wins — the
    policy is not shadowed by the old risk-based default (which would say
    local for this persona-less low-risk story)."""
    # The policy branch is gated on the operator env being unset
    # (PIPELINE_LOCAL_MAX_RISK is an explicit runtime ceiling that must win
    # over the static policy); make that precondition explicit instead of
    # silently depending on the operator's environment.
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_RISK", raising=False)
    state = {"error": None, "resolution": None, "calls": []}
    state["resolution"] = RouteResolution(
        tier="strong", provider="claude", model="claude"
    )
    _patch_resolve_route(monkeypatch, state)

    assert usage._route_dispatch_backend({"risk": "low"}) == "claude"


def test_env_ceiling_overrides_policy(monkeypatch):
    """Env precedence: PIPELINE_LOCAL_MAX_RISK set in the environment is an
    explicit operator ceiling that wins over the static routing policy — the
    legacy env-coupled tail runs and a low-risk story routes local even when
    the policy names another backend."""
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    state = {"error": None, "resolution": None, "calls": []}
    state["resolution"] = RouteResolution(
        tier="strong", provider="litellm", model="openai/gpt-5-mini"
    )
    _patch_resolve_route(monkeypatch, state)

    assert usage._route_dispatch_backend({"risk": "low"}) == "local"


def test_regression_no_policy_falls_through(monkeypatch):
    """Criterion 3: with no routing resolution, today's two-way behavior is
    byte-identical — low risk routes local, high risk routes claude."""
    state = {"error": None, "resolution": None, "calls": []}
    _patch_resolve_route(monkeypatch, state)

    assert usage._route_dispatch_backend({"risk": "low"}) == "local"
    assert usage._route_dispatch_backend({"risk": "high"}) == "claude"


def test_fail_open_on_registry_error(monkeypatch):
    """Criterion 4: a RoleRegistryError from resolve_route must not propagate
    (the scheduler keeps dispatching) and must leave nothing latched — the
    very next call with a working policy routes normally."""
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_RISK", raising=False)
    state = {"error": None, "resolution": None, "calls": []}
    state["error"] = RoleRegistryError("route 'dispatch': bad policy key")
    _patch_resolve_route(monkeypatch, state)

    assert usage._route_dispatch_backend({"risk": "low"}) == "local"

    state["error"] = None
    state["resolution"] = RouteResolution(
        tier="strong", provider="litellm", model="openai/gpt-5-mini"
    )
    assert usage._route_dispatch_backend({"risk": "low"}) == "litellm"


def test_unknown_risk_still_defaults_to_claude(monkeypatch):
    """Criterion 5 (negative): with no routing policy, today's risk handling
    is unchanged. NOTE: the story brief assumed a MISSING risk key maps to
    claude, but the pre-existing code defaults a missing key to "low"
    (`story.get("risk") or "low"`) → "local"; only an UNKNOWN risk STRING is
    treated as highest → "claude". Both facts pinned here as-is."""
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_RISK", raising=False)
    state = {"error": None, "resolution": None, "calls": []}
    _patch_resolve_route(monkeypatch, state)

    assert usage._route_dispatch_backend({"risk": "bogus"}) == "claude"
    assert usage._route_dispatch_backend({}) == "local"


def test_integration_through_real_caller(monkeypatch):
    """Criterion 6: drive dispatch._resolve_dispatch_backend, the real caller.
    The policy must be consulted for a fresh auto story, and must NOT be
    consulted when the story already carries an explicit backend (a prior
    escalation flip wins as-is)."""
    state = {"error": None, "resolution": None, "calls": []}
    state["resolution"] = RouteResolution(
        tier="strong", provider="litellm", model="openai/gpt-5-mini"
    )
    _patch_resolve_route(monkeypatch, state)

    assert (
        dispatch._resolve_dispatch_backend(
            {"persona": "software-engineer", "risk": "low"}, "auto"
        )
        == "litellm"
    )
    assert len(state["calls"]) == 1

    assert (
        dispatch._resolve_dispatch_backend(
            {"persona": "software-engineer", "risk": "low", "backend": "claude"},
            "auto",
        )
        == "claude"
    )
    assert len(state["calls"]) == 1  # policy not consulted for explicit backend