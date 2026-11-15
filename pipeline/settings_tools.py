"""MCP tools over the tenant settings records (ADR 0015).

The local operator, and any write to the host-only deployment layer, is decided
through ``authorization.refusal_for_host_config``, as it always has. Any other
write, and any read or listing by a non-operator caller, is decided per layer:
the structural agent refusal (ADR 0014 decision 3) comes first, then the
authorizer is asked ``config:write`` (or ``config:read``) on a resource naming
the layer's scope id and the repo. The transport refuses the deployment layer to
a remote caller before the handler runs (``pipeline.remote_tool_policy``). Every write and every refusal appends one
``settings:set`` record to the host audit chain. A refusal returns an explicit
``{"ok": False, "reason": ..., "error": ...}`` dict.

``get_settings`` and ``list_settings`` read the stored records only. They never
resolve layers; ``pipeline.settings_store.effective_setting`` does that.
"""

from __future__ import annotations

import logging
from typing import Any

from app.identity import LOCAL_OPERATOR, Principal

from . import authorization
from .actor import current_principal
from .audit import append_host_audit
from .authorization import _agent_structural_denial, refusal_for_host_config
from .live_ref import LiveRef
from .policy_settings import provider_configured
from .settings_registry import REGISTRY, SettingKey
from .store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError, RecordStoreError
from .store_settings import SettingsVersionConflictError, validate_settings_key

logger = logging.getLogger("pipeline.settings_tools")

ACTION = "settings:set"
#: The labels a remote settings call is decided under. ``Action`` has no read
#: label, so the read travels as the raw string the authorizer seam accepts.
CONFIG_READ = "config:read"
CONFIG_WRITE = "config:write"
#: The layer a remote caller may never read or write, whatever the authorizer says.
HOST_ONLY_SCOPE = "deployment"
SETTINGS_PLUGIN_VARIABLE = "PIPELINE_SETTINGS_PLUGIN"
#: The scopes ``PIPELINE_SETTINGS_PLUGIN`` supplies; the Store must not write them.
PLUGIN_SCOPES = ("org", "team")
#: The resolver's ``SettingKey.accepts`` only requires an int >= 1. These bounds
#: are the write contract, so ``set_setting`` never stores a value outside them.
TIMEOUT_BOUNDS = (1, 86400)
WORKER_BOUNDS = (1, 64)

_store = LiveRef("_store")


def _loggable(value: Any) -> Any:
    """Return a JSON-safe form of an untrusted argument for the audit record."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return type(value).__name__


def _bounds(setting: SettingKey) -> tuple[int, int] | None:
    if setting.type != "int":
        return None
    if setting.name.endswith("_timeout_s"):
        return TIMEOUT_BOUNDS
    if setting.name == "grade_xdist_workers":
        return WORKER_BOUNDS
    return None


def value_problem(setting: SettingKey, value: Any) -> tuple[str, str] | None:
    if not setting.accepts(value):
        return "invalid_value", f"invalid value for {setting.name!r}"
    bounds = _bounds(setting)
    if bounds is not None and not bounds[0] <= value <= bounds[1]:
        return "invalid_value", f"{setting.name!r} must be between {bounds[0]} and {bounds[1]}"
    return None


def allow_loosen_problem(allow_loosen: Any) -> tuple[str, str] | None:
    """Refuse an ``allow_loosen`` list that names anything but ceiling keys.

    The message names the first offending key and nothing else.
    """
    if allow_loosen is None:
        return None
    if not isinstance(allow_loosen, list):
        return "invalid_allow_loosen", "allow_loosen must be a list of ceiling settings keys"
    for name in allow_loosen:
        if not isinstance(name, str) or name not in REGISTRY or REGISTRY[name].kind != "ceiling":
            return "invalid_allow_loosen", f"allow_loosen must name ceiling settings keys: {_loggable(name)!r}"
    return None


def allow_loosen_layer_problem(scope: str, allow_loosen: Any) -> tuple[str, str] | None:
    """Refuse ``allow_loosen`` at the layer nothing sits below."""
    if allow_loosen is not None and scope == "team_repo":
        return "allow_loosen_layer", "allow_loosen is not writable at the team_repo scope"
    return None


def _request_problem(
    scope: Any, scope_id: Any, repo: Any, key: Any, value: Any, expected_version: Any, allow_loosen: Any
) -> tuple[str, str] | None:
    """Refuse a malformed request before any policy or store is consulted."""
    try:
        validate_settings_key(scope, scope_id, repo)
    except InvalidRecordKeyError as exc:
        return "invalid_key", str(exc)
    if not isinstance(key, str) or key not in REGISTRY:
        return "unknown_key", f"unknown settings key {_loggable(key)!r}"
    problem = value_problem(REGISTRY[key], value)
    if problem is not None:
        return problem
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return "invalid_version", "expected_version must be null or a version of 1 or more"
    return allow_loosen_problem(allow_loosen)


def _policy_problem(scope: str, key: str) -> tuple[str, str] | None:
    """Refuse a key at a scope the registry does not allow it at."""
    setting = REGISTRY[key]
    if setting.kind == "lock" and scope != "deployment":
        return "lock_key", f"{key!r} is a deployment-only lock key"
    if scope not in setting.allowed_layers:
        return "layer_not_allowed", f"{key!r} cannot be set at the {scope} scope"
    return None


def _plugin_problem(scope: str) -> tuple[str, str] | None:
    """Refuse an org or team write while a settings plugin is the source of truth.

    A probe that cannot say whether a plugin is configured counts as configured:
    the write is refused rather than split between two sources.
    """
    if scope not in PLUGIN_SCOPES:
        return None
    try:
        configured = provider_configured()
    except Exception as exc:  # noqa: BLE001 - an unknown plugin state fails closed
        logger.warning(
            "settings plugin probe failed; refusing the write",
            extra={"error_type": type(exc).__name__},
        )
        configured = True
    if configured:
        message = (
            f"{scope} settings are read from the {SETTINGS_PLUGIN_VARIABLE} provider, "
            f"so the Store cannot write them while {SETTINGS_PLUGIN_VARIABLE} is set"
        )
        return "settings_plugin", message
    return None


def _audit(outcome: str, target: dict, detail: dict) -> None:
    append_host_audit(action=ACTION, outcome=outcome, target=target, detail=detail)


def _refuse(target: dict, actor: str, value: Any, reason: str, message: str) -> dict:
    _audit("denied", target, {"actor": actor, "new": _loggable(value), "reason": reason})
    return {"ok": False, "reason": reason, "error": message}


def _not_authorized(action: str, reason: str) -> dict:
    """Return the explicit refusal for a remote caller the layer decision refused."""
    return {"ok": False, "reason": reason, "error": f"not authorized: {action} ({reason})"}


def _layer_refusal_reason(action: str, principal: Principal, scope_id: str, repo: str) -> str:
    """Return why *principal* may not act on one layer, or ``""`` when it may.

    An agent never writes configuration, and that refusal comes first, so no
    authorizer is asked for it. The authorizer then decides the layer, on a
    resource naming the layer's scope id and the repo. A raising authorizer is
    refused by ``is_allowed``.
    """
    if _agent_structural_denial(principal, action):
        return "agent_principal"
    resource = authorization.Resource(team=scope_id, repo=repo)
    if not authorization.is_allowed(authorization.default_authorizer(), principal, action, resource):
        return "not_authorized"
    return ""


def _write_refusal(scope: str, scope_id: str, repo: str, key: str) -> dict | None:
    """Return the refusal for a write, or ``None`` when it may proceed.

    The local operator, and any write to the host-only deployment layer, keep the
    host-scoped decision. Every other layer is decided on its own resource.
    """
    principal = current_principal()
    if principal is LOCAL_OPERATOR or scope == HOST_ONLY_SCOPE:
        return refusal_for_host_config(CONFIG_WRITE, f"the {scope} setting {key!r}")
    reason = _layer_refusal_reason(CONFIG_WRITE, principal, scope_id, repo)
    return _not_authorized(CONFIG_WRITE, reason) if reason else None


def remote_settings_refusal(name: str, principal: Principal, arguments: object) -> dict | None:
    """Return the refusal for a remote call to a settings tool, or ``None`` to allow.

    The remote transport asks this before the handler runs, so a refused call
    never reaches a handler. ``list_settings`` names no layer, so it is decided
    on an empty resource: a caller who may read no layer at all is refused.
    A call whose layer arguments are missing or not strings is refused.
    """
    action = CONFIG_WRITE if name == "set_setting" else CONFIG_READ
    if name == "list_settings":
        reason = _layer_refusal_reason(action, principal, "", "")
    else:
        layer = arguments if isinstance(arguments, dict) else {}
        scope, scope_id, repo = (layer.get(field) for field in ("scope", "scope_id", "repo"))
        if not all(isinstance(value, str) for value in (scope, scope_id, repo)):
            return _not_authorized(action, "invalid_request")
        if scope == HOST_ONLY_SCOPE:
            reason = "host_only_scope"
        else:
            reason = _layer_refusal_reason(action, principal, scope_id, repo)
    return _not_authorized(action, reason) if reason else None


def set_setting(
    scope: str,
    scope_id: str,
    repo: str,
    key: str,
    value: Any,
    expected_version: int | None,
    allow_loosen: list[str] | None = None,
) -> dict:
    """Write one settings key at one layer, compare-and-set on ``expected_version``.

    ``expected_version`` is ``null`` to create a record and the stored version to
    update it. ``allow_loosen`` replaces the layer's list of ceiling keys that the
    layer below may loosen; ``null`` keeps the list already stored.

    Returns ``{"ok": True, "scope", "scope_id", "repo", "key", "value",
    "new_version"}`` on success, or a refusal dict. Every outcome is audited.
    """
    actor = current_principal().subject
    target = {
        "scope": _loggable(scope),
        "scope_id": _loggable(scope_id),
        "repo": _loggable(repo),
        "key": _loggable(key),
    }

    problem = _request_problem(scope, scope_id, repo, key, value, expected_version, allow_loosen)
    if problem is None:
        problem = (
            _policy_problem(scope, key)
            or allow_loosen_layer_problem(scope, allow_loosen)
            or _plugin_problem(scope)
        )
    if problem is not None:
        return _refuse(target, actor, value, *problem)

    refusal = _write_refusal(scope, scope_id, repo, key)
    if refusal is not None:
        _audit("denied", target, {"actor": actor, "new": _loggable(value), "reason": "not_authorized"})
        return refusal

    records = _store.records()
    try:
        current = records.get_settings(DEFAULT_NAMESPACE, scope, scope_id, repo)
        old_values = current["values"] if current else {}
        old_loosen = current["allow_loosen"] if current else []
        new_loosen = sorted(set(allow_loosen)) if allow_loosen is not None else old_loosen
        stored = records.put_settings(
            DEFAULT_NAMESPACE,
            scope,
            scope_id,
            repo,
            {**old_values, key: value},
            new_loosen,
            actor,
            expected_version,
        )
    except SettingsVersionConflictError as exc:
        return _refuse(target, actor, value, "version_conflict", str(exc))
    except RecordStoreError:
        return _refuse(target, actor, value, "store_error", "the settings store could not be written")

    _audit(
        "allowed",
        target,
        {
            "actor": actor,
            "old": old_values.get(key),
            "new": _loggable(value),
            "new_version": stored["version"],
            "old_allow_loosen": old_loosen,
            "new_allow_loosen": new_loosen,
        },
    )
    return {
        "ok": True,
        "scope": scope,
        "scope_id": scope_id,
        "repo": repo,
        "key": key,
        "value": value,
        "new_version": stored["version"],
    }


def get_settings(scope: str, scope_id: str, repo: str) -> dict:
    """Return the stored record for one layer, or ``record: None`` when unset."""
    try:
        validate_settings_key(scope, scope_id, repo)
    except InvalidRecordKeyError as exc:
        return {"ok": False, "reason": "invalid_key", "error": str(exc)}
    principal = current_principal()
    if principal is not LOCAL_OPERATOR and scope != HOST_ONLY_SCOPE:
        reason = _layer_refusal_reason(CONFIG_READ, principal, scope_id, repo)
        if reason:
            return _not_authorized(CONFIG_READ, reason)
    try:
        record = _store.records().get_settings(DEFAULT_NAMESPACE, scope, scope_id, repo)
    except RecordStoreError:
        return {"ok": False, "reason": "store_error", "error": "the settings store could not be read"}
    return {"ok": True, "record": record}


def list_settings() -> dict:
    """Return every stored settings record, ordered by layer and key.

    A remote caller reads every layer but the host-only deployment layer, and
    must be authorized to read the listing as a whole.
    """
    principal = current_principal()
    remote = principal is not LOCAL_OPERATOR
    if remote:
        reason = _layer_refusal_reason(CONFIG_READ, principal, "", "")
        if reason:
            return _not_authorized(CONFIG_READ, reason)
    try:
        records = _store.records().list_settings(DEFAULT_NAMESPACE)
    except RecordStoreError:
        return {"ok": False, "reason": "store_error", "error": "the settings store could not be read"}
    if remote:
        records = [record for record in records if record["scope"] != HOST_ONLY_SCOPE]
    return {"ok": True, "records": records}


#: The tools this module provides, in registration order. ``pipeline.server``
#: registers each one on the MCP instance.
TOOLS = (set_setting, get_settings, list_settings)
