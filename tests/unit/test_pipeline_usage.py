"""Tests for pipeline/usage.py's _role_resource_ok review-gate resolution.

role_config is plan-level and OPTIONAL (pipeline-story-schema.md), so most
plans never set it and plan_role_config is routinely None. These tests guard
against a regression where a None plan_role_config skips the registry-aware
resolution entirely and falls straight to the hardcoded Claude env default,
even though the registry itself names a non-Claude review provider - which
disagrees with how _run_reviewer resolves the SAME review backend via an
unconditional role_registry.resolve_role call. Confirmed live on
overlord-failure-triage (2026-08-18): 159 "Claude usage gate tripped"
deferrals over 3.2h while the registry's roles.review was ollama/glm the
whole time.
"""
from app import role_registry
from pipeline import usage as u


class _FakeDriver:
    def __init__(self, ok, reason=""):
        self._ok = ok
        self._reason = reason

    def resource_status(self, model_tag=None):
        return {"ok": self._ok, "reason": self._reason}


def test_role_resource_ok_review_consults_registry_when_plan_role_config_is_none(
    monkeypatch,
):
    # No plan-level role_config at all (the common case) - the registry's
    # own review provider must still win over the hardcoded Claude default.
    registry = {
        "providers": {"ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}}},
        "roles": {"review": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)

    captured = {}

    def _fake_get_backend(role, name=None):
        captured["role"] = role
        captured["name"] = name
        return _FakeDriver(ok=True)

    monkeypatch.setattr(u.backend, "get_backend", _fake_get_backend)

    ok, _reason = u._role_resource_ok("review", plan_role_config=None)

    assert ok is True
    assert captured["name"] == "ollama"


def test_role_resource_ok_review_none_plan_role_config_does_not_check_claude(
    monkeypatch,
):
    # The regression this guards: a None plan_role_config must not silently
    # gate an Ollama-configured review role on Claude's usage poller.
    registry = {
        "providers": {"ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}}},
        "roles": {"review": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)

    def _claude_backend_should_not_be_used(role, name=None):
        if name == "claude":
            raise AssertionError(
                "review gate must not fall through to Claude when the "
                "registry names a non-Claude review provider, even with "
                "plan_role_config=None"
            )
        return _FakeDriver(ok=True)

    monkeypatch.setattr(u.backend, "get_backend", _claude_backend_should_not_be_used)

    ok, _reason = u._role_resource_ok("review", plan_role_config=None)

    assert ok is True


def test_role_resource_ok_review_falls_back_to_claude_when_truly_unconfigured(
    monkeypatch,
):
    # A genuinely unconfigured install (no plan role_config, no registry
    # review entry) must still resolve to the env-based Claude default -
    # this is the one case the fallback path exists for.
    registry = {"providers": {"claude": {"models": {}}}, "roles": {}}
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)

    captured = {}

    def _fake_get_backend(role, name=None):
        captured["name"] = name
        return _FakeDriver(ok=False, reason="Claude usage gate tripped")

    monkeypatch.setattr(u.backend, "get_backend", _fake_get_backend)

    ok, reason = u._role_resource_ok("review", plan_role_config=None)

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_role_resource_ok_review_explicit_plan_role_config_still_wins(monkeypatch):
    # Regression sanity: an explicit plan_role_config override (the
    # already-working case) must keep beating the registry. A model must
    # accompany the provider override here - _role_resource_ok calls
    # resolve_role with no model_fallback, so a provider switch with no
    # matching model raises RoleRegistryError and falls through to the
    # env-based path, which would pass this assertion for the wrong reason.
    registry = {
        "providers": {
            "ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}},
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet"}}},
        },
        "roles": {"review": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)

    captured = {}

    def _fake_get_backend(role, name=None):
        captured["name"] = name
        return _FakeDriver(ok=True)

    monkeypatch.setattr(u.backend, "get_backend", _fake_get_backend)

    u._role_resource_ok(
        "review",
        plan_role_config={"review": {"provider": "claude", "model": "sonnet"}},
    )

    assert captured["name"] == "claude"


def test_role_resource_ok_review_gates_on_the_model_review_will_actually_use(
    monkeypatch,
):
    """The gate must ask about the resolved review MODEL, not the env default.

    Live 2026-10-01, gemma4_26b bench arm: the plan pinned dispatch to a 15GB
    on-device model and every other role to a cloud model. resolve_role
    correctly returned ollama for review, but the gate then called
    resource_status() with no model_tag, so OllamaDriver fell back to
    PIPELINE_LOCAL_MODEL_DEFAULT - the arm's dispatch model - and refused
    review as "14909mb weights > 60% of 24576mb" on every tick, while
    _run_reviewer was resolving that same review to the cloud model and would
    never have loaded those weights at all. Same defect class as the backend
    mismatch this module's other tests guard, one layer deeper: here the gate
    and the reviewer disagreed about the model.
    """
    registry = {
        "providers": {"ollama": {"models": {"cloudrev": {"tag": "cloudrev:cloud"}}}},
        "roles": {},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "big-on-device:latest")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)

    captured = {}

    class _CapturingDriver(_FakeDriver):
        def resource_status(self, model_tag=None):
            captured["model_tag"] = model_tag
            return {"ok": True, "reason": ""}

    monkeypatch.setattr(
        u.backend, "get_backend", lambda role, name=None: _CapturingDriver(ok=True)
    )

    u._role_resource_ok(
        "review",
        plan_role_config={"review": {"provider": "ollama", "model": "cloudrev"}},
    )

    assert captured["model_tag"] == "cloudrev:cloud"
