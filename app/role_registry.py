"""Central registry for per-role provider/model resolution across the
pipeline's roles (overlord, planner, dispatch, review, decompose).

model_registry.json (repo root, or PIPELINE_MODEL_REGISTRY_PATH) declares
two things in one discoverable place: which models exist per provider
(claude/ollama/mlx/lmstudio), and which provider/model each role defaults
to. Neither section is required — a missing file, or a role missing from
`roles`, falls through to the caller's own existing default, so shipping
this file (or leaving `roles` empty) changes no behavior.

resolve_role() only ever supplies a *default*. It never overrides a value
a caller already resolved for a more specific reason (an escalation
forcing Claude, an explicit name= override, a rework's pinned backend) —
callers consult it only in their own "nothing more specific configured"
fallback branch, and existing role-specific env vars keep the same
priority they already have (checked by the caller, or passed in via
model_fallback, before resolve_role's own registry step runs).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"
_DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "model_registry.json"


class RoleRegistryError(ValueError):
    """model_registry.json is malformed, or a roles.* entry names a
    provider/model not declared under providers.* — fail closed rather
    than silently resolving to some other model, since a typo'd name must
    never go unnoticed."""


@dataclass(frozen=True)
class RoleResolution:
    provider: str
    model: str


@dataclass(frozen=True)
class RouteResolution:
    tier: str
    provider: str
    model: str


def _registry_path() -> Path:
    override = os.environ.get(_REGISTRY_PATH_ENV)
    return Path(override) if override else _DEFAULT_REGISTRY_PATH


def load_registry(path: Path | None = None) -> dict:
    """Load and validate model_registry.json.

    A missing file is not an error — the registry is fully optional — and
    returns {}. Malformed JSON, or a roles.* entry naming a provider/model
    not declared under providers.*, raises RoleRegistryError naming the
    exact bad key.
    """
    target = path if path is not None else _registry_path()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text())
    except json.JSONDecodeError as e:
        raise RoleRegistryError(f"{target}: invalid JSON ({e})") from e

    providers = data.get("providers", {})
    for role_name, role_cfg in data.get("roles", {}).items():
        provider = role_cfg.get("provider")
        if provider is None:
            continue
        if provider not in providers:
            raise RoleRegistryError(
                f"roles.{role_name} names unknown provider {provider!r} "
                f"(not declared under providers)"
            )
        model = role_cfg.get("model")
        if model is not None and model not in providers[provider].get("models", {}):
            raise RoleRegistryError(
                f"roles.{role_name} names unknown model {model!r} for "
                f"provider {provider!r} (not declared under "
                f"providers.{provider}.models)"
            )
    return data


def _fallback_value(model_fallback: str | Callable[[], str | None] | None) -> str | None:
    return model_fallback() if callable(model_fallback) else model_fallback


def resolve_role(
    role: str,
    *,
    plan_role_config: dict | None = None,
    model_fallback: str | Callable[[], str | None] | None = None,
    registry: dict | None = None,
    default_provider: str = "claude", environ: dict | None = None,
) -> RoleResolution:
    """Resolve (provider, model) for `role`.

    Provider priority: plan_role_config[role]["provider"] ->
    PIPELINE_BACKEND_<ROLE> env var (the same var/default get_backend()
    itself applies) -> registry["roles"][role]["provider"] ->
    default_provider ("claude", matching get_backend()'s own default,
    unless a caller has its own bottom-of-chain default - e.g. the
    planner role mirroring whichever provider dispatch already picked).

    Model priority: plan_role_config[role]["model"] ->
    registry["roles"][role]["model"] -> model_fallback (the caller's own
    bespoke default: persona frontmatter, DEFAULT_MODEL, etc.).

    Both plan- and registry-supplied model values are *friendly names*,
    resolved against providers.<resolved provider>.models — never a raw
    tag — so a typo is caught here, not deep inside a driver. The
    registry's (provider, model) pairing for a role is only used when the
    registry's own provider for that role is the one that actually won;
    if a higher-priority source (plan/env) overrides the provider, the
    registry's model pairing (which belongs to its own provider) is
    ignored in favor of model_fallback, rather than raising against a
    provider it was never paired with.
    """
    if environ is None:
        environ = os.environ
    reg = registry if registry is not None else load_registry()
    plan_cfg = (plan_role_config or {}).get(role, {})
    reg_role_cfg = reg.get("roles", {}).get(role, {})

    provider = (
        plan_cfg.get("provider")
        or environ.get(f"PIPELINE_BACKEND_{role.upper()}")
        or reg_role_cfg.get("provider")
        or default_provider
    ).strip().lower()

    registry_model = None
    if reg_role_cfg.get("model") and reg_role_cfg.get("provider") in (None, provider):
        registry_model = reg_role_cfg["model"]
    model_name = plan_cfg.get("model") or registry_model

    if model_name:
        provider_models = reg.get("providers", {}).get(provider, {}).get("models", {})
        if model_name not in provider_models:
            raise RoleRegistryError(
                f"role {role!r} resolved to provider {provider!r} with "
                f"model {model_name!r}, which is not declared under "
                f"providers.{provider}.models"
            )
        model = provider_models[model_name]["tag"]
    else:
        model = _fallback_value(model_fallback)
        if not model:
            raise RoleRegistryError(
                f"role {role!r}: no model configured (plan_role_config, "
                f"registry, and the caller's fallback are all empty)"
            )

    return RoleResolution(provider=provider, model=model)


def resolve_route(
    role: str,
    *,
    story: dict | None = None,
    registry: dict | None = None,
    plan_role_config: dict | None = None,
) -> RouteResolution | None:
    """Resolve a routing tier for `role` from the registry's optional
    `routing` block (plan-level `routing` beats the registry's own, mirroring
    how plan_role_config beats the registry in resolve_role).

    A missing routing block, or a role missing from it, is NOT an error —
    returns None so the caller keeps its own existing behavior untouched
    (the registry is fully optional, like load_registry()).

    Resolution: evaluate `rules` IN ORDER and take the FIRST whose `when`
    clause matches the story dict; otherwise fall back to `default_tier`;
    then look the chosen tier up in `tiers` and resolve provider+model
    through the same validation resolve_role performs — the tier's model is
    a friendly name checked against providers.<provider>.models, and the
    returned `model` is that entry's concrete `tag`, never the friendly
    name.

    Supported `when` predicates, exactly these two: `max_risk` (matches when
    the story's risk is at or below the named level; a missing/unknown risk
    is treated as the HIGHEST risk, fail closed) and `persona` (exact string
    match on story["persona"]). Any other predicate key raises
    RoleRegistryError naming the exact bad key — never silently ignored,
    since that would make a rule appear to apply when it does not.
    """
    reg = registry if registry is not None else load_registry()
    routing = (plan_role_config or {}).get("routing") or reg.get("routing") or {}
    route_cfg = routing.get(role)
    if not route_cfg:
        return None

    tiers = route_cfg.get("tiers", {})
    rules = route_cfg.get("rules", [])
    story = story or {}

    # Risk ranking for the max_risk predicate — a local copy of
    # pipeline.config._RISK_ORDER rather than an import: pipeline.* modules
    # import app.role_registry (dispatch, overlord, planner, review, ...), so
    # importing pipeline back from here would create a circular import.
    risk_order = {"low": 0, "medium": 1, "high": 2}

    # Fail closed on the WHOLE block before evaluating anything: a typo in a
    # later rule (unknown predicate, unknown tier) must never lurk silently
    # until the day it becomes the first match.
    default_tier = route_cfg.get("default_tier")
    if default_tier not in tiers:
        raise RoleRegistryError(
            f"routing.{role} default_tier {default_tier!r} is not declared "
            f"under routing.{role}.tiers"
        )
    for rule in rules:
        for predicate, value in rule.get("when", {}).items():
            if predicate == "max_risk":
                if str(value).lower() not in risk_order:
                    raise RoleRegistryError(
                        f"routing.{role} rule names unknown max_risk level "
                        f"{value!r} (expected one of "
                        f"{sorted(risk_order)})"
                    )
            elif predicate != "persona":
                raise RoleRegistryError(
                    f"routing.{role} rule uses unrecognized 'when' predicate "
                    f"{predicate!r} (supported: max_risk, persona)"
                )
        if rule.get("tier") not in tiers:
            raise RoleRegistryError(
                f"routing.{role} rule names tier {rule.get('tier')!r}, which "
                f"is not declared under routing.{role}.tiers"
            )

    chosen_tier = default_tier
    for rule in rules:
        when = rule.get("when", {})
        for predicate, value in when.items():
            if predicate == "max_risk":
                max_rank = risk_order[str(value).lower()]
                story_rank = risk_order.get(
                    str(story.get("risk") or "").lower(), risk_order["high"]
                )
                if story_rank > max_rank:
                    break
            elif story.get("persona") != value:
                break
        else:
            chosen_tier = rule["tier"]
            break

    tier_cfg = tiers[chosen_tier]
    provider = str(tier_cfg.get("provider") or "").strip().lower()
    providers = reg.get("providers", {})
    if provider not in providers:
        raise RoleRegistryError(
            f"routing.{role}.tiers.{chosen_tier} names unknown provider "
            f"{provider!r} (not declared under providers)"
        )
    model_name = tier_cfg.get("model")
    provider_models = providers[provider].get("models", {})
    if model_name not in provider_models:
        raise RoleRegistryError(
            f"routing.{role}.tiers.{chosen_tier} names unknown model "
            f"{model_name!r} for provider {provider!r} (not declared under "
            f"providers.{provider}.models)"
        )
    return RouteResolution(
        tier=chosen_tier, provider=provider, model=provider_models[model_name]["tag"]
    )
