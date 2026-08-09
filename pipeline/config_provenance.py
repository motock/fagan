import json
import os
from pathlib import Path

IGNORED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("PIPELINE_TRANSPORT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
)

def read_mcp_server_env(path: Path | None = None, *, server_name: str = "pipeline") -> dict[str, str]:
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        mcp_servers = data.get("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            return {}
        server_cfg = mcp_servers.get(server_name, {})
        if not isinstance(server_cfg, dict):
            return {}
        env = server_cfg.get("env", {})
        if not isinstance(env, dict):
            return {}
        return {k: str(v) for k, v in env.items()}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}

def _scheduler_plist_path() -> Path:
    env_path = os.environ.get("PIPELINE_SCHEDULER_PLIST_PATH")
    if env_path:
        return Path(env_path)
    return Path.home() / "Library" / "LaunchAgents" / "com.claude.pipeline.advance-scheduler.plist"

def resolve_role_provenance(
    role: str,
    *,
    plan_role_config: dict | None = None,
    registry: dict | None = None,
    model_fallback: str | None = None,
    environ: dict[str, str] | None = None,
) -> dict:
    if environ is None:
        environ = os.environ
    if plan_role_config is None:
        plan_role_config = {}
    if registry is None:
        provider_source = "default_provider"
        model_source = ""
        restart_required = False
        return {
            "error": f"Role {role} has no model configured",
            "provider_source": provider_source,
            "model_source": model_source,
            "restart_required": restart_required,
        }

    # Walk precedence: plan_role_config -> env -> registry -> default
    provider = None
    provider_source = ""
    if role in plan_role_config:
        provider = plan_role_config[role].get("provider")
        provider_source = "plan_role_config"
    elif "PIPELINE_BACKEND_" + role.upper() in environ:
        provider = environ["PIPELINE_BACKEND_" + role.upper()]
        provider_source = f"env:PIPELINE_BACKEND_{role.upper()}"
    else:
        provider = registry.get(role, {}).get("provider")
        provider_source = "model_registry.json"

    model = None
    model_source = ""
    if role in plan_role_config and "model" in plan_role_config[role]:
        model = plan_role_config[role]["model"]
        model_source = "plan_role_config"
    elif "PIPELINE_MODEL_" + role.upper() in environ:
        model = environ["PIPELINE_MODEL_" + role.upper()]
        model_source = f"env:PIPELINE_MODEL_{role.upper()}"
    else:
        model = registry.get(role, {}).get("model")
        model_source = "model_registry.json"

    # Resolve final provider/model via role_registry
    try:
        from app import role_registry as role_registry_mod  # type: ignore
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

    restart_required = False
    restart_required = False
    if provider != final_provider:
        restart_required = True
    elif provider == "claude" and environ.get("PIPELINE_BACKEND_OVERLORD"):
        restart_required = True
    elif provider == "claude" and environ.get("PIPELINE_BACKEND_OVERLORD"):
        restart_required = True

    return {
        "provider": final_provider,
        "model": final_model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": restart_required,
        "error": None,
    }
