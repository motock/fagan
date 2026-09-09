"""
Module providing read‑only accessors for configuration values from three sources:

* The launchd scheduler plist's ``EnvironmentVariables`` block.
* The MCP server's ``env`` block in the user’s ~/.claude.json file.
* Code defaults (not implemented here – this module only reads files).

The module is intentionally lightweight and imports only standard‑library modules. It must not import any of the orchestrator or dashboard code to avoid import cycles.

It also defines the list of transport-only environment variables that are
overwritten by :mod:`app.backend` on every dispatch, and a helper function to
report which ones are present in a given environment. These are consumed both
by the backend at import time (to warn operators) and by the effective-config
view, so there is exactly one definition - a second copy would drift.
"""
from __future__ import annotations

import json
import os
import pathlib
import plistlib
import xml.parsers.expat
from dataclasses import dataclass

Path = pathlib.Path

# The six transport-only env vars that backend.py overwrites on every dispatch.
# These must be kept in sync with the warning loop in app/backend.py.
IGNORED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("PIPELINE_TRANSPORT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
)

# The exact reason string used in the warning message.
_REASON = (
    "transport-only value backend.py overwrites on every dispatch "
    "- it has no effect as an input"
)


def ignored_env_vars_present(environ: dict | None = None) -> list[dict]:
    """Return a list of dicts describing transport-only env vars present.

    Parameters
    ----------
    environ:
        Mapping of environment variable names to values.  If ``None`` the
        function reads :data:`os.environ`.

    Returns
    -------
    list[dict]
        Each dict contains ``name``, ``use_instead`` and ``reason`` keys.
    """
    if environ is None:
        environ = os.environ
    result: list[dict] = []
    for name, replacement in IGNORED_ENV_VARS:
        if name in environ:
            result.append(
                {
                    "name": name,
                    "use_instead": replacement,
                    "reason": _REASON,
                }
            )
    return result


# ---------------------------------------------------------------------------
# Path helpers – lazily resolve env overrides so that tests can monkeypatch the
# environment or ``Path.home`` without affecting module import time.
# ---------------------------------------------------------------------------

def _scheduler_plist_path() -> Path:
    """Return the path to the launchd scheduler plist.

    If ``PIPELINE_SCHEDULER_PLIST_PATH`` is set, that value is used.  Otherwise
    the default is ``~/Library/LaunchAgents/com.claude.pipeline.advance-scheduler.plist``.
    The resolution happens inside the function so callers can monkeypatch the
    environment after import.
    """
    env_path = os.getenv("PIPELINE_SCHEDULER_PLIST_PATH")
    if env_path:
        return Path(env_path).resolve()
    # Default path – use ``Path.home`` at call time to allow tests to monkeypatch it.
    default = Path.home() / "Library" / "LaunchAgents" / "com.claude.pipeline.advance-scheduler.plist"
    return default.resolve()


def _claude_json_path() -> Path:
    """Return the path to the MCP server JSON configuration file.

    If ``PIPELINE_CLAUDE_JSON_PATH`` is set, that value is used.  Otherwise the
    default is ``~/.claude.json``.
    """
    env_path = os.getenv("PIPELINE_CLAUDE_JSON_PATH")
    if env_path:
        return Path(env_path).resolve()
    default = Path.home() / ".claude.json"
    return default.resolve()

# ---------------------------------------------------------------------------
# Diagnostic readers – never raise, always return ``dict[str,str]``.
# ---------------------------------------------------------------------------

def read_plist_env(path: Path | None = None) -> dict[str, str]:
    """Read the ``EnvironmentVariables`` dictionary from a launchd plist.

    Parameters
    ----------
    path:
        The file to read.  If ``None`` the default scheduler plist is used.

    Returns
    -------
    dict[str, str]
        Mapping of environment variable names to string values.  All values are
        coerced with ``str()``.  On any error (file missing, permission
        denied, malformed XML, etc.) an empty dictionary is returned.
    """
    if path is None:
        path = _scheduler_plist_path()
    try:
        data_bytes = path.read_bytes()
        data = plistlib.loads(data_bytes)
    except (OSError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError, ValueError):
        return {}
    # ``data`` should be a dict; if not, bail.
    if not isinstance(data, dict):
        return {}
    env = data.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return {}
    # Coerce all values to str.
    return {k: str(v) for k, v in env.items()}


def read_mcp_server_env(path: Path | None = None, server_name: str = "pipeline") -> dict[str, str]:
    """Read the ``env`` block from a MCP server JSON configuration.

    Parameters
    ----------
    path:
        The file to read.  If ``None`` the default ~/.claude.json is used.
    server_name:
        Name of the server entry to look up under ``mcpServers``.

    Returns
    -------
    dict[str, str]
        Mapping of environment variable names to string values.  All values are
        coerced with ``str()``.  On any error an empty dictionary is returned.
    """
    if path is None:
        path = _claude_json_path()
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {}
    # Validate structure step by step.
    if not isinstance(payload, dict):
        return {}
    mcp_servers = payload.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return {}
    server_entry = mcp_servers.get(server_name)
    if not isinstance(server_entry, dict):
        return {}
    env_block = server_entry.get("env")
    if not isinstance(env_block, dict):
        return {}
    return {k: str(v) for k, v in env_block.items()}

# End of module.


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    default: str | None

ENV_VAR_CATALOG: tuple[EnvVarSpec, ...] = (
    # Config file env vars
    EnvVarSpec("PIPELINE_AUTONOMY", "gated"),
    EnvVarSpec("PIPELINE_RISK_THRESHOLD", "low"),
    EnvVarSpec("PIPELINE_DEFAULT_MODEL", "sonnet"),
    EnvVarSpec("PIPELINE_PAUSE_THRESHOLD", "90"),
    EnvVarSpec("PIPELINE_RESUME_THRESHOLD", "70"),
    EnvVarSpec("PIPELINE_WEEK_PAUSE_THRESHOLD", "90"),
    EnvVarSpec("PIPELINE_WEEK_RESUME_THRESHOLD", "70"),
    EnvVarSpec("PIPELINE_USAGE_STALE_AFTER_SECONDS", "1800"),
    EnvVarSpec("PIPELINE_DAILY_REQUEST_THRESHOLD", "3000"),
    EnvVarSpec("PIPELINE_WEEKLY_REQUEST_THRESHOLD", "15000"),
    EnvVarSpec("USAGE_BLIND_PAUSE_AFTER_SECONDS", "21600"),
    EnvVarSpec("USAGE_BLIND_LOG_INTERVAL", "60"),
    EnvVarSpec("PIPELINE_MAX_CONCURRENT_AGENTS", "3"),
    EnvVarSpec("PIPELINE_MERGE_MAX_ATTEMPTS", "3"),
    EnvVarSpec("PIPELINE_DISPATCH_MAX_ATTEMPTS", "3"),
    EnvVarSpec("PIPELINE_DISPATCH_STARTUP_GRACE_SECONDS", "90"),
    EnvVarSpec("PIPELINE_DISPATCH_WATCHDOG_SECONDS", "3600"),
    EnvVarSpec("PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS", "1800"),
    EnvVarSpec("PIPELINE_STEP_CAP_FALLBACK_THRESHOLD", "3"),
    EnvVarSpec("PIPELINE_INFRA_FAILURE_FALLBACK_THRESHOLD", "3"),
    EnvVarSpec("PIPELINE_LOCAL_MAX_RISK", "low"),
    EnvVarSpec("PIPELINE_REWORK_MAX_ATTEMPTS", "3"),
    EnvVarSpec("PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE", "1"),
    EnvVarSpec("PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED", "3"),
    EnvVarSpec("PIPELINE_REWORK_MAX_ATTEMPTS_NO_COMMIT", "2"),
    EnvVarSpec("PIPELINE_REVIEW_INCONCLUSIVE_MAX", "2"),
    EnvVarSpec("PIPELINE_PLANE_MAX_ATTEMPTS", "3"),
    # Reviewer auto-fix vars
    EnvVarSpec("PIPELINE_REVIEWER_AUTO_FIX", "0"),
    EnvVarSpec("PIPELINE_REVIEWER_AUTO_FIX_MAX_FILES", "1"),
    EnvVarSpec("PIPELINE_REVIEWER_AUTO_FIX_MAX_LINES", "40"),
    EnvVarSpec("PIPELINE_REVIEWER_INLINE_DIFF_MAX_CHARS", "40000"),
    # Extra vars
    EnvVarSpec("PIPELINE_BACKEND_DISPATCH", "claude"),
    EnvVarSpec("PIPELINE_LOCAL_PROVIDER", "ollama"),
    EnvVarSpec("PIPELINE_LOCAL_MAX_STEPS", "40"),
    EnvVarSpec("PIPELINE_LOCAL_NUM_CTX", "16384"),
    EnvVarSpec("PIPELINE_LOCAL_TEMPERATURE", "0.3"),
    EnvVarSpec("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b"),
    # Cloud-model relaxation: a ":cloud"-tagged model proxied through the
    # local endpoint is a frontier model, not a constrained on-device one.
    # These override the on-device LOCAL_* knobs ONLY for ":cloud" tags so a
    # capable cloud model gets a raised context ceiling and step cap and the
    # weak-model park guards disabled (see backend_ollama._tuned_num_ctx and
    # OllamaDriver.dispatch). On-device dispatch is unaffected.
    EnvVarSpec("PIPELINE_CLOUD_NUM_CTX", "131072"),
    EnvVarSpec("PIPELINE_CLOUD_MAX_STEPS", "120"),
    EnvVarSpec("PIPELINE_AUTO_ESCALATE", None),
    # Escalation retarget: backend/model a stuck story escalates TO (default
    # Claude, no model override). See _escalation_target in escalation.py.
    EnvVarSpec("PIPELINE_ESCALATION_BACKEND", "claude"),
    EnvVarSpec("PIPELINE_ESCALATION_MODEL", None),
    # Wedge detector thresholds
    EnvVarSpec("PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS", "3600"),
    EnvVarSpec("PIPELINE_WEDGE_SCAN_ENABLED", "1"),
    EnvVarSpec("PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS", "1800"),
    # Path variables (see pipeline/paths.py) — catalogued so they appear in
    # the effective-config view. Defaults mirror paths.py exactly.
    EnvVarSpec("PLAN_DIR", "~/.claude/plans"),
    EnvVarSpec("WORKTREE_ROOT", "~/.claude/worktrees"),
    EnvVarSpec("AGENTS_DIR", "~/.claude/agents"),
    EnvVarSpec("OVERLORD_POLICY", "~/.claude/overlord-policy.md"),
    EnvVarSpec("USAGE_STATE_PATH", "~/.claude/usage_state.json"),
)

def _is_secret(name: str) -> bool:
    return any(sub in name for sub in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))


def resolve_env_var(name, default=None, *, environ=None, plist_env=None, mcp_env=None):
    if environ is None:
        environ = os.environ
    if plist_env is None:
        plist_env = read_plist_env()
    if mcp_env is None:
        mcp_env = read_mcp_server_env()

    effective = environ.get(name, default)


    if name in environ:
        if name in plist_env and plist_env[name] == environ[name]:
            source = "launchd_plist"
        elif name in mcp_env and mcp_env[name] == environ[name]:
            source = "mcp_server_env"
        else:
            source = "process_env"
    else:
        source = "code_default"

    conflict = (
        name in plist_env
        and name in mcp_env
        and plist_env[name] != mcp_env[name]
    )

    layers: list[dict[str, object]] = []
    if name in environ:
        layers.append(
            {"layer": "process_env", "value": environ[name], "restart_required": True}
        )
    if name in plist_env:
        layers.append(
            {
                "layer": "launchd_plist",
                "value": plist_env[name],
                "restart_required": True,
            }
        )
    if name in mcp_env:
        layers.append(
            {"layer": "mcp_server_env", "value": mcp_env[name], "restart_required": True}
        )
    layers.append({"layer": "code_default", "value": default, "restart_required": False})

    restart_required = source != "code_default"

    masked = _is_secret(name)
    if masked:
        # Only an environment-supplied value is sensitive; a code default is
        # public (it lives in this repo), so `effective` keeps the raw default
        # when the var is absent from environ. Layer values are always masked.
        if name in environ:
            effective = "***"
        for layer in layers:
            layer["value"] = "***"

    for layer in layers:
        layer["effective_in_process"] = layer["layer"] == source

    return {
        "name": name,
        "effective": effective,
        "source": source,
        "restart_required": restart_required,
        "conflict": conflict,
        "masked": masked,
        "layers": layers
}

PIPELINE_ROLES: tuple[str, ...] = (
    "overlord",
    "planner",
    "dispatch",
    "review",
    "decompose",
    "test_author",
    "diagnosis",
    "security",
    "chat",
)

def effective_env_config(*, environ=None, plist_env=None, mcp_env=None):
    """Return a diagnostic list for all catalog environment variables.

    Parameters
    ----------
    environ : Mapping[str, str] | None
        Environment mapping to use.  If ``None`` defaults to ``os.environ``.
    plist_env : dict[str,str] | None
        Pre‑read launchd plist values.  If ``None`` the module will read from
        the default plist file once.
    mcp_env : dict[str,str] | None
        Pre‑read MCP server env block.  If ``None`` the module will read from
        the default JSON file once.

    Returns
    -------
    list[dict]
        One dictionary per catalog entry, sorted by variable name.
    """
    if environ is None:
        environ = os.environ
    if plist_env is None:
        plist_env = read_plist_env()
    if mcp_env is None:
        mcp_env = read_mcp_server_env()
    # Resolve each catalog entry using the same env snapshots.
    results: list[dict] = []
    for spec in sorted(ENV_VAR_CATALOG, key=lambda s: s.name):
        results.append(
            resolve_env_var(spec.name, default=spec.default,
                            environ=environ, plist_env=plist_env, mcp_env=mcp_env)
        )
    return results

def resolve_role_provenance(
    role,
    *,
    plan_role_config=None,
    registry=None,
    model_fallback=None,
    environ=None,
) -> dict:
    """
    Resolve the provider and model for a role.

    The final provider/model come from :func:`app.role_registry.resolve_role`
    (the canonical resolver, also used by planner.py/server.py/review.py/
    rebrief.py/usage.py) so there is a single source of truth for the actual
    decision. The precedence walk below is retained ONLY to label which layer
    produced the winning value (``provider_source``/``model_source``) and
    whether a restart is required — information ``resolve_role`` does not
    return. The two error paths (model-not-in-registry, no-model-configured)
    keep their pre-existing ``{"error": ...}`` shape and wording: a
    ``RoleRegistryError`` raised by ``resolve_role`` is caught and translated
    back into this function's own error dict rather than propagated.

    Parameters
    ----------
    role : str
        The role name to resolve.
    plan_role_config : Mapping[str, Mapping[str, str]] | None
        Optional per‑role configuration from the launch plan.  Keys are role names; values may contain ``provider`` and/or ``model`` strings.
    registry : Mapping[str, Any] | None
        Optional model registry data structure.  Expected keys: ``roles`` mapping to provider/model pairs and ``providers`` mapping to provider→models→tag.
    model_fallback : str | Callable[[], str] | None
        Value or callable used when no other source supplies a model.
    environ : Mapping[str, str] | None
        Environment dictionary.  If ``None`` the real :data:`os.environ` is used.

    Returns
    -------
    dict
        A mapping with keys:
            role, provider, model,
            provider_source, model_source,
            restart_required, error.
    """
    if environ is None:
        environ = os.environ
    if plan_role_config is None:
        plan_role_config = {}
    if registry is None:
        registry = {}

    # Local walk: labels ONLY (provider_source / model_source / restart_required).
    # Mirrors the same precedence chain resolve_role applies internally, so the
    # label always describes the layer that actually won.
    registry_roles = registry.get("roles", {})
    if not isinstance(registry_roles, dict):
        registry_roles = {}

    provider_value = None
    provider_source = "unset"
    if role in plan_role_config and isinstance(plan_role_config[role], dict) and "provider" in plan_role_config[role]:
        provider_value = plan_role_config[role]["provider"]
        provider_source = "plan_role_config"
    elif f"PIPELINE_BACKEND_{role.upper()}" in environ:
        provider_value = environ[f"PIPELINE_BACKEND_{role.upper()}"]
        provider_source = f"env:PIPELINE_BACKEND_{role.upper()}"
    elif role in registry_roles and isinstance(registry_roles[role], dict):
        if "provider" in registry_roles[role]:
            provider_value = registry_roles[role]["provider"]
            provider_source = "model_registry.json"
    if not provider_value:
        provider_value = "claude"
        provider_source = "default"

    provider_value = provider_value.strip().lower()
    restart_required = provider_source.startswith("env:")

    model_source = "unset"
    if role in plan_role_config and isinstance(plan_role_config[role], dict) and "model" in plan_role_config[role]:
        model_source = "plan_role_config"
    elif role in registry_roles and isinstance(registry_roles[role], dict) and ("model" in registry_roles[role] and (registry_roles[role].get("provider") is None or registry_roles[role]["provider"] == provider_value)):
        model_source = "model_registry.json"
    elif model_fallback is not None:
        model_source = "caller_fallback"

    from app import role_registry

    try:
        resolution = role_registry.resolve_role(
            role,
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=model_fallback,
            environ=environ,
        )
    except role_registry.RoleRegistryError as exc:
        if "no model configured" in str(exc):
            # The role resolved to a provider fine; only the model is absent.
            # Keep the provider and its source label so a caller can still
            # report WHERE the provider came from, and mark the model "unset"
            # rather than null - a role with no model configured is a
            # reportable diagnostic state, not an unresolvable one.
            return {
                "role": role,
                "provider": provider_value,
                "model": None,
                "provider_source": provider_source,
                "model_source": "unset",
                "restart_required": restart_required,
                "error": f"Role {role} has no model configured",
            }
        # A model was named but is not declared for the resolved provider:
        # nothing about this role resolved cleanly, so report no sources.
        return {
            "role": role,
            "provider": None,
            "model": None,
            "provider_source": None,
            "model_source": None,
            "restart_required": False,
            "error": f"Role {role} has provider {provider_value!r} not declared in registry",
        }

    return {
        "role": role,
        "provider": resolution.provider,
        "model": resolution.model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": restart_required,
        "error": None,
    }


def effective_role_config(*, plan_role_config=None, registry=None, model_fallbacks=None, environ=None):
    """
    Return a diagnostic list for all roles in PIPELINE_ROLES.

    Parameters
    ----------
    plan_role_config : Mapping[str, Mapping[str, str]] | None
        Optional per‑role configuration from the launch plan.
    registry : Mapping[str, Any] | None
        Optional model registry data structure.  If ``None`` a fresh registry is loaded once.
    model_fallbacks : Mapping[str, str | Callable[[], str]] | None
        Per‑role fallback value or callable used when no other source supplies a model.
    environ : Mapping[str, str] | None
        Environment dictionary.  If ``None`` the real :data:`os.environ` is used.

    Returns
    -------
    list[dict]
        One dictionary per role in ``PIPELINE_ROLES``.
    """
    if registry is None:
        registry = {}
    results: list[dict] = []
    for role_name in PIPELINE_ROLES:
        fallback = None
        if model_fallbacks and role_name in model_fallbacks:
            fallback = model_fallbacks[role_name]
        result = resolve_role_provenance(
            role_name,
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=fallback,
            environ=environ,
        )
        results.append(result)
    return results
