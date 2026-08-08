from __future__ import annotations

import app.role_registry

PIPELINE_ROLES = (
    "overlord",
    "planner",
    "dispatch",
    "review",
    "decompose",
    "test_author",
    "diagnosis",
    "security",
)

def resolve_role_provenance(
    role: str,
    *,
    plan_role_config=None,
    registry=None,
    model_fallback=None,
    environ=None,
) -> dict:
    # Determine provider source
    if plan_role_config and isinstance(plan_role_config.get(role), dict) and "provider" in plan_role_config[role]:
        provider_source = "plan_role_config"
    elif environ is not None and f"PIPELINE_BACKEND_{role.upper()}" in environ:
        provider_source = f"env:PIPELINE_BACKEND_{role.upper()}"
    elif registry is not None and role in registry.get("roles", {}):
        provider_source = "model_registry.json"
    else:
        provider_source = "default"

    # Determine model source
    if plan_role_config and isinstance(plan_role_config.get(role), dict) and "model" in plan_role_config[role]:
        model_source = "plan_role_config"
    elif registry is not None and role in registry.get("roles", {}):
        model_source = "model_registry.json"
    elif model_fallback is not None:
        model_source = "caller_fallback"
    else:
        model_source = "unset"

    restart_required = provider_source.startswith("env:")

    # Determine provider_value for boundary case
    if plan_role_config and isinstance(plan_role_config.get(role), dict) and "provider" in plan_role_config[role]:
        provider_value = plan_role_config[role]["provider"].strip().lower()
    elif environ is not None and f"PIPELINE_BACKEND_{role.upper()}" in environ:
        provider_value = environ[f"PIPELINE_BACKEND_{role.upper()}"] .strip().lower()
    elif registry is not None and role in registry.get("roles", {}):
        provider_value = registry["roles"][role].get("provider","") .strip().lower()
    else:
        provider_value = "claude"

    try:
        resolved = app.role_registry.resolve_role(
            role=role,
            plan_role_config=plan_role_config or {},
            registry=registry or {},
            environ=environ or {},
        )
        provider = getattr(resolved, "provider", None)
        model = getattr(resolved, "model", None)
    except app.role_registry.RoleRegistryError as e:
        if model_source == "unset":
            return {
                "role": role,
                "provider": provider_value,
                "model": None,
                "provider_source": provider_source,
                "model_source": "unset",
                "restart_required": restart_required,
                "error": str(e),
            }
        else:
            return {
                "role": role,
                "provider": None,
                "model": None,
                "provider_source": None,
                "model_source": None,
                "restart_required": False,
                "error": str(e),
            }

    return {
        "role": role,
        "provider": provider,
        "model": model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": restart_required,
        "error": None,
    }
