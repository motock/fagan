import json
import os
from pathlib import Path
import plistlib
import xml.parsers.expat

# Import role registry components
import app.role_registry as role_registry_mod


def read_plist_env(path: Path | None = None) -> dict[str, str]:
    """Read EnvironmentVariables from a launchd plist.

    Returns an empty dict on any error or if the key is missing.
    All values are coerced to strings.
    """
    if path is None:
        return {}
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
        if not isinstance(data, dict):
            return {}
        env = data.get("EnvironmentVariables", {})
        if not isinstance(env, dict):
            return {}
        return {k: str(v) for k, v in env.items()}
    except (OSError, xml.parsers.expat.ExpatError, plistlib.InvalidFileException):
        return {}


def read_mcp_server_env(path: Path | None = None) -> dict[str, str]:
    """Read the ``env`` dictionary from an MCP server JSON file.

    Returns an empty dict on any error or if the key is missing.
    All values are coerced to strings.
    """
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env", {})
        if not isinstance(env, dict):
            return {}
        return {k: str(v) for k, v in env.items()}
    except (OSError, json.JSONDecodeError):
        return {}


# Resolve role provenance logic

def resolve_role_provenance(
    role: str,
    *,
    plan_role_config: dict | None = None,
    registry: dict | None = None,
    model_fallback: str | None = None,
    environ: dict | None = None,
) -> dict:
    """Resolve the effective provider and model for a role.

    The function delegates to ``app.role_registry.resolve_role`` after performing
    local precedence logic to determine source labels.  Errors from the registry
    are translated into the legacy error shape used by callers.
    """
    if environ is None:
        environ = os.environ
    if plan_role_config is None:
        plan_role_config = {}
    if registry is None:
        # Legacy behaviour: return error when no registry provided
        provider_source = "default_provider"
        model_source = ""
        restart_required = False
        return {
            "error": f"Role {role} has provider claude not declared in registry",
            "provider_source": provider_source,
            "model_source": model_source,
            "restart_required": restart_required,
        }

    # 1) Provider precedence: plan_role_config -> environ -> registry -> default
    provider = (
        plan_role_config.get("provider")
        or environ.get(f"PIPELINE_BACKEND_{role.upper()}_PROVIDER")
        or registry.get("providers", {}).get(role, {}).get("default_provider")
    )
    if not provider:
        provider = "claude"
        provider_source = "default_provider"
    else:
        provider_source = "plan_role_config" if plan_role_config.get("provider") else (
            f"env:{environ.get(f'PIPELINE_BACKEND_{role.upper()}_PROVIDER')}" if environ.get(f'PIPELINE_BACKEND_{role.upper()}_PROVIDER') else "model_registry.json"
        )

    # 2) Model precedence: plan_role_config -> environ -> registry_model -> fallback
    model_name = None
    reg_role_cfg = registry.get("roles", {}).get(role, {})
    if plan_role_config.get("model"):
        model_name = plan_role_config["model"]
        model_source = "plan_role_config"
    elif environ.get(f"PIPELINE_BACKEND_{role.upper()}_MODEL"):
        model_name = environ[f"PIPELINE_BACKEND_{role.upper()}_MODEL"]
        model_source = f"env:{environ.get(f'PIPELINE_BACKEND_{role.upper()}_MODEL')}"
    elif reg_role_cfg.get("model"):
        model_name = reg_role_cfg["model"]
        model_source = "model_registry.json"
    else:
        model_name = None
        model_source = ""

    # 3) Resolve final provider/model via role_registry
    try:
        res = role_registry_mod.resolve_role(
            role,
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=model_fallback,
            environ=environ,
        )
        final_provider = res.provider
        final_model = res.model
    except role_registry_mod.RoleRegistryError as exc:
        msg = str(exc)
        if "no model configured" in msg:
            return {
                "error": f"Role {role} has no model configured",
                "provider_source": provider_source,
                "model_source": model_source,
                "restart_required": False,
            }
        elif "not declared" in msg:
            return {
                "error": f"Role {role} has provider {provider} not declared in registry",
                "provider_source": provider_source,
                "model_source": model_source,
                "restart_required": False,
            }
        else:
            return {
                "error": msg,
                "provider_source": provider_source,
                "model_source": model_source,
                "restart_required": False,
            }

    # 4) Determine if restart required: provider changed from default or env
    restart_required = False
    if provider != final_provider:
        restart_required = True
    elif provider == "claude" and environ.get(f"PIPELINE_BACKEND_{role.upper()}_PROVIDER"):
        restart_required = True

    return {
        "role": role,
        "provider": final_provider,
        "model": final_model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": restart_required,
        "error": None,
    }
