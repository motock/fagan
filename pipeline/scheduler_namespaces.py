"""The scheduler tick's advance across namespaces.

The default namespace advances exactly as ``server.advance_all_plans`` always
has. Each other namespace that holds plans is then advanced inside
``bind_namespace``, one plan at a time, through its own record view. A plan
whose manifest names a different namespace from the one it is stored under is
refused and audited, and is not advanced.

An install with no record view, or whose only namespace is the default, takes
the unchanged path and gets back the unchanged result object.

``pipeline.server`` is resolved at call time so tests that patch its
``advance_all_plans`` or ``advance_pipeline`` see the patch.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from pipeline import advance_pool
from pipeline.actor import bind_namespace, current_namespace
from pipeline.namespace_guard import audit_refusal, resolve_child_namespace
from pipeline.scheduler_scope import SchedulerScope, SchedulerScopeError, load_scope
from pipeline.store_namespaces import namespaces_with_plans

logger = logging.getLogger("pipeline.scheduler_namespaces")


def _record_view(server: Any) -> Any | None:
    """Return the store's namespaced record view, or ``None`` when it has none."""
    records = getattr(getattr(server, "_store", None), "records", None)
    return records() if callable(records) else None


def _advance_namespace(server: Any, records: Any, namespace: str) -> tuple[dict, list[str]]:
    """Advance every plan stored in *namespace*; return results and refusal reasons.

    Must run with *namespace* bound on the in-process path: ``advance_pipeline``
    reads and writes through the bound namespace. The pooled path passes
    *namespace* to a child process instead, which binds it itself.
    """
    plans: dict[str, dict] = {}
    refusals: list[str] = []
    pooled = advance_pool.pooled_advance_enabled()
    for plan_name in records.list_plans(namespace):
        try:
            manifest = records.get_manifest(namespace, plan_name) or {}
            _, reason = resolve_child_namespace(manifest, namespace, namespace)
            if reason is not None:
                refusals.append(reason)
                plans[plan_name] = {"ok": False, "error": reason}
                continue
            if pooled:
                plans[plan_name] = advance_pool.advance_plan_subprocess(
                    plan_name, advance_pool._plan_timeout_seconds(), namespace=namespace
                )
            else:
                plans[plan_name] = server.advance_pipeline(plan_name)
        except Exception as exc:  # noqa: BLE001 - reported by class name, one plan only
            plans[plan_name] = {"ok": False, "error": f"advance raised {type(exc).__name__}"}
    return plans, refusals


#: Cursor rotated by one namespace per pass, so no namespace is always walked
#: first (and therefore never always the one a deadline cuts off).
_namespace_rotation = 0


def _rotated(namespaces: list[str]) -> list[str]:
    """Return *namespaces* starting at the rotation cursor, which then advances by one."""
    global _namespace_rotation
    if not namespaces:
        return []
    start = _namespace_rotation % len(namespaces)
    _namespace_rotation += 1
    return namespaces[start:] + namespaces[:start]


def _advance_others(server: Any, records: Any, others: list[str]) -> dict[str, dict]:
    """Advance each namespace in *others* under its own binding; return per-namespace results."""
    namespaces: dict[str, dict] = {}
    for namespace in _rotated(others):
        with bind_namespace(namespace):
            plans, refusals = _advance_namespace(server, records, namespace)
        for reason in refusals:
            audit_refusal(reason)
        namespaces[namespace] = {"ok": True, "plans": plans}
    return namespaces


def _scoped_others(records: Any, scope: SchedulerScope) -> list[str]:
    """Return the non-default namespaces the scope admits, reading nothing it excludes.

    An ONLY scope names its namespaces directly, so the store is not asked to
    enumerate. A SKIP scope filters the enumeration before any per-namespace read.
    """
    default = current_namespace()
    if scope.only is not None:
        return [namespace for namespace in scope.only if namespace != default]
    return [
        namespace
        for namespace in namespaces_with_plans(records)
        if namespace != default and scope.allows(namespace)
    ]


def _advance_in_scope(
    server: Any, advance_default: Callable[[], dict[str, Any]], scope: SchedulerScope
) -> dict[str, Any]:
    """Advance only the namespaces *scope* admits; the default advance runs only when admitted."""
    records = _record_view(server)
    result = advance_default() if scope.allows(current_namespace()) else {"ok": True, "plans": {}}
    if records is None:
        return result

    try:
        others = _scoped_others(records, scope)
    except Exception as exc:  # noqa: BLE001 - enumeration failure is reported, default still runs
        logger.error("namespace enumeration failed error_class=%s", type(exc).__name__)
        result["namespace_error"] = "namespace_enumeration_failed"
        return result

    if others:
        result["namespaces"] = _advance_others(server, records, others)
    return result


def advance_all_namespaces(advance_default: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Advance the default namespace via *advance_default*, then every other namespace.

    *advance_default* is ``server.advance_all_plans``, passed in by the daemon's
    composition root so the default path stays the same function object.

    An invalid scope from the environment refuses the whole tick: nothing is
    advanced and the result carries ``namespace_error``.
    """
    from pipeline import server

    try:
        scope = load_scope(os.environ)
    except SchedulerScopeError as exc:
        logger.error("scheduler scope refused error_class=%s", type(exc).__name__)
        return {"ok": False, "plans": {}, "namespace_error": "scheduler_scope_invalid"}
    if scope is not None:
        return _advance_in_scope(server, advance_default, scope)

    records = _record_view(server)
    if records is None:
        return advance_default()

    try:
        others = [
            namespace
            for namespace in namespaces_with_plans(records)
            if namespace != current_namespace()
        ]
    except Exception as exc:  # noqa: BLE001 - enumeration failure is reported, default still runs
        logger.error("namespace enumeration failed error_class=%s", type(exc).__name__)
        result = advance_default()
        result["namespace_error"] = "namespace_enumeration_failed"
        return result

    result = advance_default()
    if not others:
        return result

    result["namespaces"] = _advance_others(server, records, others)
    return result
