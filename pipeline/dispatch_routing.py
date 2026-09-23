"""Backend/target routing helpers for story dispatch.

Extracted verbatim from pipeline/dispatch.py. The moved bodies resolve their
free names against THIS module's globals, so every name they read that is a
module-level binding of pipeline.dispatch is bound here as a _ModuleRef: the
reference reads pipeline.dispatch's attribute at call time, so a
monkeypatch.setattr on either module keeps landing.
"""

import logging
from typing import Any

from app import role_registry

from .module_ref import _ModuleRef

_route_dispatch_backend = _ModuleRef("pipeline.dispatch", "_route_dispatch_backend")
_persona_requires_claude = _ModuleRef("pipeline.dispatch", "_persona_requires_claude")
_story_has_unwinnable_local_scope = _ModuleRef(
    "pipeline.dispatch", "_story_has_unwinnable_local_scope"
)


def _resolve_dispatch_backend(story: dict[str, Any], env_backend: str) -> str:
    """Resolve the concrete backend name for a story's dispatch.

    Shared by dispatch_story (which persists the result onto the story) and
    the per-story dispatch gate in _advance_pipeline_locked (which must gate
    each story by ITS OWN backend+model, not a blanket env-default gate).
    Priority order:
      1. story["backend"] already set (e.g. from an escalation flip)
      2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
      3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
    Then the persona-based and unwinnable-scope safety overrides (both only
    when the story had no explicit backend, so a prior escalation flip wins
    as-is and is never re-routed here).
    """
    dispatch_backend = story.get("backend") or (
        _route_dispatch_backend(story) if env_backend == "auto" else env_backend
    )
    # Persona-based safety override: a security persona always dispatches to
    # Claude, regardless of dispatch mode (auto/local/claude) - unless the
    # story already had an explicit backend (a prior escalation flip), which
    # wins as-is and is never re-routed here.
    if not story.get("backend") and _persona_requires_claude(story):
        dispatch_backend = "claude"
    # Unwinnable-as-scoped safety override: a repo-wide, unscoped lint/fix
    # sweep always dispatches to Claude too, for the same reason (Mode 40
    # retro #4) - see _story_has_unwinnable_local_scope's docstring.
    if not story.get("backend") and _story_has_unwinnable_local_scope(story):
        dispatch_backend = "claude"
    return dispatch_backend



def _dispatch_fallback_provider(plan_role_config: dict[str, Any] | None) -> str:
    """Provider-only dispatch resolution for the fail-open path.

    Keeps the exact pre-registry priority - plan role_config, then the
    dispatch env var, then "claude" - without a raw env read here:
    resolve_role applies the env var's own priority itself. The provider is
    resolved against the REAL registry, so a provider-only roles.dispatch
    entry (provider set, model absent) selects the registry provider instead
    of falling through to "claude". The registry's MODEL pairing is never
    trusted on this path - the sentinel model_fallback stands in for it and
    is discarded - so a poisoned model can never crash dispatch. If the
    registry entry is malformed (it names a model that is not declared
    under its provider), resolve_role raises and this degrades to the
    pre-registry resolution against an empty registry: plan role_config,
    then the env var, then "claude".
    """
    plan_provider = ((plan_role_config or {}).get("dispatch") or {}).get("provider")
    try:
        return role_registry.resolve_role(
            "dispatch",
            plan_role_config=(
                {"dispatch": {"provider": plan_provider}} if plan_provider else None
            ),
            model_fallback=lambda: "unpinned",
        ).provider
    except role_registry.RoleRegistryError:
        # Malformed roles.dispatch entry: fall back to the pre-registry
        # priority, resolved against an empty registry so the poisoned
        # entry supplies nothing at all.
        return role_registry.resolve_role(
            "dispatch",
            plan_role_config=(
                {"dispatch": {"provider": plan_provider}} if plan_provider else None
            ),
            registry={"providers": {}, "roles": {}},
            model_fallback=lambda: "unpinned",
        ).provider


def _registry_tag_for(provider: str, model: str) -> str:
    """Translate a bare registry model NAME to its concrete tag.

    ``model`` is returned unchanged when it already looks like a tag
    (contains ':' or '/'), or when the live registry has no
    ``providers.<provider>.models.<model>.tag`` entry, or when the registry
    cannot be loaded. A bare name that is neither a registry name nor one of
    the tier names ``opus``/``sonnet``/``haiku`` is logged as a warning,
    because the local driver would otherwise run the default model instead.
    """
    if ":" in model or "/" in model:
        return model
    try:
        registry = role_registry.load_registry()
        return registry["providers"][provider]["models"][model]["tag"]
    except role_registry.RoleRegistryError:
        return model
    except (KeyError, TypeError):
        pass
    if model not in ("opus", "sonnet", "haiku"):
        logging.getLogger("pipeline").warning(
            "story model %s is not a registry name for provider %s; "
            "the local driver will run its default model",
            model,
            provider,
        )
    return model


def _resolve_dispatch_target(
    story: dict[str, Any], plan_role_config: dict[str, Any] | None = None
) -> tuple[str, str | None]:
    """Resolve the concrete (provider, model) a story's dispatch runs on.

    REG-1: dispatch used to read PIPELINE_BACKEND_DISPATCH raw and never
    resolved a MODEL at all, so the model fell all the way through to the
    driver, which picks PIPELINE_LOCAL_MODEL_DEFAULT. On a host whose
    model_registry pins roles.dispatch the registry entry was dead config:
    get_effective_config reported the registry's model while every agent
    booted on the driver's env default (measured 2026-09-16). The pair now
    comes from role_registry.resolve_role("dispatch", ...) - the same
    resolver planner, review, test_author, overlord, rebrief and usage
    already use.

    _resolve_dispatch_backend keeps its existing override contract on top of
    the resolved provider, so every pre-registry override still wins exactly
    as it does today: story["backend"] (how an escalation flip pins a
    story), the security-persona and unwinnable-scope safety overrides, and
    PIPELINE_BACKEND_DISPATCH=auto routing through _route_dispatch_backend
    (resolve_role applies the env var's own priority - plan role_config ->
    env -> registry -> "claude" - so the env is still honoured without a
    raw read here).

    Model priority: story["model"] (the most specific pin - an escalation
    flip writes a concrete tag there) > the model resolve_role resolved
    (plan role_config, then the registry) > None, which leaves the driver's
    own env default in charge exactly as before REG-1.

    The registry's model pairing is only honoured when the registry's own
    provider is the one that actually won (resolve_role already enforces
    that pairing). story["model"] is returned UNCONDITIONALLY, though - it
    is the most specific pin and is trusted as-is, so a story whose
    story["model"] names a tag belonging to a provider that did NOT win
    (e.g. {"persona": "security-engineer", "model": "glm-5.3-flash:cloud"}
    resolving to a claude dispatch) returns ("claude",
    "glm-5.3-flash:cloud"). Only the registry/plan-resolved model is
    provider-checked: a claude dispatch is never handed an ollama tag that
    came from the registry.

    Fail-open contract: a fresh clone ships model_registry.json with no
    roles.dispatch entry, so resolve_role raises RoleRegistryError (no model
    configured anywhere). That must degrade to the pre-registry behaviour -
    the env provider, then "claude", with no model - never crash dispatch.
    The registry's MODEL pairing is not trusted on this path (the sentinel
    model_fallback stands in for it and is discarded), but the registry's
    PROVIDER still applies its normal priority, so a provider-only
    roles.dispatch entry selects the registry provider rather than falling
    through to "claude" - see _dispatch_fallback_provider.

    Shared with the per-story dispatch gate in pipeline/advance.py so the
    gate can never gate on one model while dispatch runs another.
    """
    try:
        resolution = role_registry.resolve_role(
            "dispatch", plan_role_config=plan_role_config
        )
    except role_registry.RoleRegistryError:
        # Two distinct failure shapes:
        # - A fresh clone (no roles.dispatch entry anywhere) hits this on
        #   every dispatch; that is the pre-registry status quo, so it is
        #   not worth a warning per story per tick.
        # - A malformed roles.dispatch entry (e.g. an undeclared model name)
        #   must stay loud: fail open, but surface why. load_registry itself
        #   validates roles.* entries, so it raises for exactly this shape -
        #   a load failure is then proof the entry exists and is poisoned.
        try:
            poisoned_entry = bool(
                role_registry.load_registry().get("roles", {}).get("dispatch")
            )
        except role_registry.RoleRegistryError:
            poisoned_entry = True
        if poisoned_entry:
            logging.getLogger("pipeline").warning(
                "role_registry could not resolve the dispatch role; failing "
                "open to the pre-registry behaviour (PIPELINE_BACKEND_DISPATCH, "
                "then 'claude') with no model pin",
                exc_info=True,
            )
        return (
            _resolve_dispatch_backend(
                story, _dispatch_fallback_provider(plan_role_config)
            ),
            None,
        )

    provider = _resolve_dispatch_backend(story, resolution.provider)
    model = story.get("model")
    if model:
        model = _registry_tag_for(provider, model)
    if not model and provider == resolution.provider:
        model = resolution.model
    return provider, model or None


