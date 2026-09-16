"""TDD suite: dispatch must resolve its (provider, model) through
``app.role_registry.resolve_role``, exactly like every other pipeline role.

ROOT CAUSE (measured 2026-09-16)
--------------------------------
``pipeline/dispatch.py`` and ``pipeline/advance.py`` read
``PIPELINE_BACKEND_DISPATCH`` raw and never resolved a MODEL at all, so the
model fell all the way through to the driver, which picks
``PIPELINE_LOCAL_MODEL_DEFAULT``. ``model_registry.local.json`` pins
``roles.dispatch = {provider: ollama, model: deepseek-v4.1-flash}`` and
``get_effective_config`` duly reports ``deepseek-v4.1-flash:cloud`` - yet
every agent actually booted on ``glm-5.3-flash:cloud``. The registry entry
for the single most important role was dead config, and the diagnostic tool
whose whole job is reporting what will run reported something false.

CONTRACT PINNED BY THIS FILE (the implementation must provide it)
-----------------------------------------------------------------
``pipeline.dispatch._resolve_dispatch_target(story, plan_role_config=None)``
returns ``(provider, model)``:

* ``provider`` is the post-override backend - it is
  ``_resolve_dispatch_backend(story, <provider from resolve_role>)``, so
  ``story["backend"]``, the security-persona override, the unwinnable-scope
  override and ``PIPELINE_BACKEND_DISPATCH=auto`` routing all keep working
  exactly as they do today.
* ``model`` is ``story["model"]`` when the story pins one, else the model
  ``resolve_role`` resolved (plan ``role_config`` / registry), else ``None``.
  A model belonging to a provider that did NOT win is never returned - a
  claude dispatch must never be handed an ollama tag.
* A ``RoleRegistryError`` (a fresh clone: ``model_registry.json`` ships
  ``roles: {}``, so there is no model anywhere) must fail OPEN to today's
  behaviour - the env provider, then ``"claude"``, with model ``None`` -
  never crash dispatch.

``pipeline.advance`` must resolve the per-story dispatch gate through the
SAME function, so the gate can never gate on one model while dispatch runs
another.

Per ``.claude/rules/testing-config-gates.md`` every test here stubs the
registry with a synthetic fixture; nothing asserts against this machine's
real ``model_registry``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import role_registry
from pipeline import advance, dispatch

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Synthetic registry payload. Deliberately NOT this machine's real registry:
# the point is that the *registry* wins, not that any particular tag does.
_PROVIDERS = {
    "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
    "ollama": {
        "models": {
            "deepseek-v4.1-flash": {"tag": "deepseek-v4.1-flash:cloud"},
            "glm": {"tag": "glm-5.3-flash:cloud"},
        }
    },
}

# The measured bug: the scheduler plist's PIPELINE_LOCAL_MODEL_DEFAULT.
_DRIVER_ENV_DEFAULT = "glm-5.3-flash:cloud"

_REGISTRY_DISPATCH = {"provider": "ollama", "model": "deepseek-v4.1-flash"}
_REGISTRY_MODEL_TAG = "deepseek-v4.1-flash:cloud"


def _install_registry(monkeypatch, tmp_path, roles):
    """Point the registry at a synthetic payload and drop the memo."""
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({"providers": _PROVIDERS, "roles": roles}))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    role_registry.reset_registry_cache()
    return path


def _resolve(story, plan_role_config=None):
    """Call the pinned seam, failing with a clear message if it is absent."""
    fn = getattr(dispatch, "_resolve_dispatch_target", None)
    assert fn is not None, (
        "pipeline.dispatch._resolve_dispatch_target(story, plan_role_config=None) "
        "-> (provider, model) is missing: dispatch must resolve its provider AND "
        "model through app.role_registry.resolve_role('dispatch', ...) instead of "
        "reading PIPELINE_BACKEND_DISPATCH raw and letting the driver pick the model"
    )
    return fn(story, plan_role_config=plan_role_config)


# ---------------------------------------------------------------------------
# 1. POSITIVE: registry wins when the env var is unset
# ---------------------------------------------------------------------------


def test_registry_provider_and_model_win_when_env_unset(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    provider, model = _resolve({})

    assert provider == "ollama", (
        "with PIPELINE_BACKEND_DISPATCH unset, dispatch must resolve its provider "
        "through role_registry (registry roles.dispatch.provider), not default to claude"
    )
    assert model == _REGISTRY_MODEL_TAG, (
        "dispatch must resolve its MODEL through role_registry too - today it "
        "resolves no model at all and the driver decides"
    )


# ---------------------------------------------------------------------------
# 2. POSITIVE: the measured bug - registry model beats the driver's env default
# ---------------------------------------------------------------------------


def test_registry_model_beats_pipeline_local_model_default(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    # The scheduler plist's value - the model every agent actually booted on.
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _DRIVER_ENV_DEFAULT)

    provider, model = _resolve({})

    assert provider == "ollama"
    assert model == _REGISTRY_MODEL_TAG, (
        f"the registry pins roles.dispatch to {_REGISTRY_MODEL_TAG!r}; "
        f"PIPELINE_LOCAL_MODEL_DEFAULT={_DRIVER_ENV_DEFAULT!r} must NOT win - that "
        "is the measured bug (registry entry dead, driver env default used instead)"
    )
    assert model != _DRIVER_ENV_DEFAULT


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL: an escalation flip still pins the story
# ---------------------------------------------------------------------------


def test_story_backend_still_wins_over_registry(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    provider, model = _resolve({"backend": "claude"})

    assert provider == "claude", (
        "story['backend'] (how an escalation flip pins a story) must still win "
        "over the registry-resolved provider"
    )
    assert model is None, (
        "a claude-routed story must never be handed the ollama registry tag "
        f"{_REGISTRY_MODEL_TAG!r} - the registry model belongs to a provider that "
        "did not win, so it must be dropped"
    )


def test_story_model_still_wins_over_registry_and_plan(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    story = {"model": "glm-5.3-flash:cloud"}
    plan = {"dispatch": {"provider": "ollama", "model": "deepseek-v4.1-flash"}}

    provider, model = _resolve(story, plan_role_config=plan)

    assert provider == "ollama"
    assert model == "glm-5.3-flash:cloud", (
        "story['model'] is the most specific pin (an escalation flip writes a "
        "concrete tag there) and must still beat both the plan role_config and "
        "the registry"
    )


# ---------------------------------------------------------------------------
# 4. NEGATIVE CONTROL: plan role_config still wins over the registry
# ---------------------------------------------------------------------------


def test_plan_role_config_still_wins_over_registry(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    plan = {"dispatch": {"provider": "ollama", "model": "glm"}}

    provider, model = _resolve({}, plan_role_config=plan)

    assert (provider, model) == ("ollama", "glm-5.3-flash:cloud"), (
        "a plan's role_config['dispatch'] must still win over the registry, "
        "mirroring how review.py/test_author.py pass plan_role_config through"
    )


# ---------------------------------------------------------------------------
# 5. NEGATIVE CONTROL: the security-persona safety override still forces claude
# ---------------------------------------------------------------------------


def test_security_persona_still_forces_claude(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    provider, model = _resolve({"persona": "security-engineer"})

    assert provider == "claude", (
        "a security persona must always dispatch to claude regardless of what "
        "the registry pins for roles.dispatch"
    )
    assert model != _REGISTRY_MODEL_TAG, (
        "the ollama registry tag must not leak into a claude dispatch"
    )
    assert model is None


# ---------------------------------------------------------------------------
# 6. BOUNDARY: no roles.dispatch entry -> today's fallback (env, then claude)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value,expected_provider",
    [(None, "claude"), ("ollama", "ollama"), ("claude", "claude")],
)
def test_no_registry_dispatch_entry_falls_back_to_env_then_claude(
    monkeypatch, tmp_path, env_value, expected_provider
):
    # A fresh clone: model_registry.json ships `roles: {}`.
    _install_registry(monkeypatch, tmp_path, {})
    if env_value is None:
        monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", env_value)

    provider, model = _resolve({})

    assert provider == expected_provider, (
        "with no roles.dispatch entry the provider must fall back exactly as "
        "today: PIPELINE_BACKEND_DISPATCH, then 'claude'"
    )
    assert model is None, (
        "with no model configured anywhere the resolved model must be None so "
        "the driver keeps its existing default - a fresh clone must still work"
    )


def test_auto_still_routes_through_the_apriori_router(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {})
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(dispatch, "_route_dispatch_backend", lambda story: "ollama")

    provider, model = _resolve({})

    assert provider == "ollama", (
        "PIPELINE_BACKEND_DISPATCH=auto must still route through "
        "_route_dispatch_backend - the resolved value is compared to 'auto', "
        "the raw env string is no longer read for provider selection"
    )
    assert model is None


# ---------------------------------------------------------------------------
# 7. BOUNDARY: the advance.py gate resolves the SAME target as dispatch_story
# ---------------------------------------------------------------------------


def test_advance_gate_resolves_same_target_as_dispatch(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    gate = getattr(advance, "_resolve_dispatch_target", None)
    assert gate is not None, (
        "pipeline/advance.py must resolve the per-story dispatch gate through the "
        "same _resolve_dispatch_target seam dispatch_story uses, so the gate can "
        "never gate on one model while dispatch runs another"
    )

    plan = {"dispatch": {"provider": "ollama", "model": "glm"}}
    for story in ({}, {"backend": "claude"}, {"persona": "security-engineer"}):
        assert gate(story, plan_role_config=plan) == _resolve(
            story, plan_role_config=plan
        ), f"advance gate and dispatch_story disagree for story {story!r}"

    assert gate({}) == _resolve({})


# ---------------------------------------------------------------------------
# 8. BOUNDARY: malformed registry fails open, never crashes dispatch
# ---------------------------------------------------------------------------


def test_malformed_registry_fails_open(monkeypatch, tmp_path):
    # roles.dispatch names a model that is not declared under
    # providers.ollama.models -> resolve_role raises RoleRegistryError.
    _install_registry(
        monkeypatch, tmp_path, {"dispatch": {"provider": "ollama", "model": "nope"}}
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    provider, model = _resolve({})

    assert provider == "claude", (
        "a misconfigured roles.dispatch must fail open to the pre-registry "
        "behaviour (env, then 'claude') rather than crash dispatch_story"
    )
    assert model is None


# ---------------------------------------------------------------------------
# 9. _resolve_dispatch_backend's contract is preserved
# ---------------------------------------------------------------------------


def test_resolve_dispatch_backend_contract_preserved(monkeypatch):
    assert dispatch._resolve_dispatch_backend({"backend": "claude"}, "ollama") == "claude"
    assert dispatch._resolve_dispatch_backend({}, "ollama") == "ollama"
    assert dispatch._resolve_dispatch_backend({}, "claude") == "claude"
    assert (
        dispatch._resolve_dispatch_backend({"persona": "security-engineer"}, "ollama")
        == "claude"
    )

    monkeypatch.setattr(dispatch, "_route_dispatch_backend", lambda story: "ollama")
    assert dispatch._resolve_dispatch_backend({}, "auto") == "ollama"


# ---------------------------------------------------------------------------
# 10. MECHANICAL: no raw env read for provider selection remains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["pipeline/dispatch.py", "pipeline/advance.py"])
def test_no_raw_env_read_for_dispatch_provider(rel):
    src = (_REPO_ROOT / rel).read_text()

    offenders = [
        (lineno, line.strip())
        for lineno, line in enumerate(src.splitlines(), 1)
        if "PIPELINE_BACKEND_DISPATCH" in line and "environ" in line
    ]
    assert not offenders, (
        f"{rel} still reads the dispatch provider raw from the environment: "
        f"{offenders} - provider selection must go through "
        "role_registry.resolve_role('dispatch', ...)"
    )

    assert 'os.environ.get("PIPELINE_BACKEND_DISPATCH"' not in src
    assert "os.environ.get('PIPELINE_BACKEND_DISPATCH'" not in src
    assert 'os.environ["PIPELINE_BACKEND_DISPATCH"]' not in src


def test_dispatch_role_goes_through_resolve_role():
    dispatch_src = (_REPO_ROOT / "pipeline/dispatch.py").read_text()
    assert re.search(r'resolve_role\(\s*["\']dispatch["\']', dispatch_src), (
        "pipeline/dispatch.py must call role_registry.resolve_role('dispatch', "
        "plan_role_config=..., ...) - the same resolver every other role uses"
    )

    advance_src = (_REPO_ROOT / "pipeline/advance.py").read_text()
    assert "_resolve_dispatch_target" in advance_src or re.search(
        r'resolve_role\(\s*["\']dispatch["\']', advance_src
    ), (
        "pipeline/advance.py's per-story dispatch gate must resolve through the "
        "same dispatch-role resolution as dispatch_story"
    )


def test_resolved_model_is_plumbed_into_the_dispatch_spec():
    src = (_REPO_ROOT / "pipeline/dispatch.py").read_text()

    assert "_resolve_dispatch_target" in src, (
        "dispatch_story must actually call the resolver, not just define it"
    )
    assert re.search(
        r'(spec|story)\s*\[\s*["\']model["\']\s*\]\s*='
        r'|(spec|story)\.setdefault\(\s*["\']model["\']'
        r'|["\']model["\']\s*:\s*[A-Za-z_][A-Za-z0-9_]*model[A-Za-z0-9_]*',
        src,
    ), (
        "the resolved model must be fed into the spec/story model path so a story "
        "with no explicit model gets the REGISTRY's model instead of the driver's "
        "PIPELINE_LOCAL_MODEL_DEFAULT"
    )
