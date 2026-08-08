"""
Pipeline configuration provenance utilities.

This module implements the logic required by the unit tests for
``pipeline/config_provenance.py``.  It intentionally keeps imports very
light‑weight – only ``app.role_registry`` is imported from the application
code base; no server or backend modules are touched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app import role_registry

# Import the role registry – this is a stdlib‑only leaf of the repo and
# provides ``resolve_role`` and ``load_registry``.

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------
PIPELINE_ROLES: tuple[str, ...] = (
    "overlord",
    "planner",
    "dispatch",
    "review",
    "decompose",
    "test_author",
    "diagnosis",
    "security",
)

# ---------------------------------------------------------------------------
# Helper dataclass for the provenance result – used only internally.
# ---------------------------------------------------------------------------
@dataclass
class _ProvenanceResult:
    role: str
    provider: str | None
    model: str | None
    provider_source: str | None
    model_source: str | None
    restart_required: bool
    error: str | None

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------
def resolve_role_provenance(
    role: str,
    *,
    plan_role_config: dict | None = None,
    registry: dict | None = None,
    model_fallback: str | None = None,
    environ: dict | None = None,
) -> dict:
    """Return provenance information for *role*.

    Parameters are intentionally the same as those used by the unit tests.
    ``environ`` is a mapping that will temporarily override the real
    environment when calling :func:`app.role_registry.resolve_role`.
    """

    # Resolve the registry once if not supplied.
    if registry is None:
        registry = role_registry.load_registry()

    # ---------------------------------------------------------------------
    # Determine provider source.
    # ---------------------------------------------------------------------
    provider_source: str | None = None
    if (
        plan_role_config is not None
        and isinstance(plan_role_config, dict)
        and role in plan_role_config
        and isinstance(plan_role_config[role], dict)
        and "provider" in plan_role_config[role]
    ):
        provider_source = "plan_role_config"
    elif environ is not None and f"PIPELINE_BACKEND_{role.upper()}" in environ:
        provider_source = f"env:PIPELINE_BACKEND_{role.upper()}"
    elif (
        isinstance(registry, dict)
        and "roles" in registry
        and role in registry["roles"]
        and isinstance(registry["roles"][role], dict)
        and "provider" in registry["roles"][role]
    ):
        provider_source = "model_registry.json"
    else:
        provider_source = "default"

    # ---------------------------------------------------------------------
    # Determine model source.
    # ---------------------------------------------------------------------
    model_source: str | None = None
    if (
        plan_role_config is not None
        and isinstance(plan_role_config, dict)
        and role in plan_role_config
        and isinstance(plan_role_config[role], dict)
        and "model" in plan_role_config[role]
    ):
        model_source = "plan_role_config"
    elif (
        isinstance(registry, dict)
        and "roles" in registry
        and role in registry["roles"]
        and isinstance(registry["roles"][role], dict)
        and "model" in registry["roles"][role]
    ):
        model_source = "model_registry.json"
    elif model_fallback is not None:
        model_source = "caller_fallback"
    else:
        model_source = "unset"

    restart_required = provider_source.startswith("env:") if provider_source else False

    # ---------------------------------------------------------------------
    # Call resolve_role – temporarily patch os.environ.
    # ---------------------------------------------------------------------
    original_environ = os.environ.copy()
    try:
        if environ is not None:
            os.environ.update(environ)
        rr = role_registry.resolve_role(
            role,
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=model_fallback,
        )
        provider = rr.provider
        model = rr.model
    except role_registry.RoleRegistryError as e:
        # Fail‑open – return diagnostic information.
        os.environ.clear()
        os.environ.update(original_environ)
        # Determine default provider from registry if available
        if registry is not None:
            default_provider = registry.get("default_provider")
        else:
            default_provider = role_registry.load_registry().get("default_provider")

        if default_provider is None:
            try:
                default_provider = role_registry.load_registry().get("default_provider")
            except role_registry.RoleRegistryError:
                default_provider = None
        return {
            "role": role,
            "provider": default_provider,
            "model": None,
            "provider_source": None,
            "model_source": model_source,
            "restart_required": False,
            "error": str(e),
        }
    finally:
        os.environ.clear()
        os.environ.update(original_environ)

    return {
        "role": role,
        "provider": provider,
        "model": model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": restart_required,
        "error": None,
    }

# ---------------------------------------------------------------------------
# Aggregator – not used by the current tests but part of the public API.
# ---------------------------------------------------------------------------
def effective_role_config(
    *,
    plan_role_config: dict | None = None,
    registry: dict | None = None,
    model_fallbacks: dict[str, str] | None = None,
    environ: dict | None = None,
) -> list[dict]:
    """Return a list of provenance dictionaries for all pipeline roles.

    The function loads the registry once and re‑uses it for every role.
    ``model_fallbacks`` is a mapping from role name to a fallback model string.
    """

    if registry is None:
        registry = role_registry.load_registry()

    results: list[dict] = []
    for role in PIPELINE_ROLES:
        mf = None
        if model_fallbacks is not None:
            mf = model_fallbacks.get(role)
        results.append(
            resolve_role_provenance(
                role,
                plan_role_config=plan_role_config,
                registry=registry,
                model_fallback=mf,
                environ=environ,
            )
        )
    return results

# ---------------------------------------------------------------------------
# End of module.
# ---------------------------------------------------------------------------
