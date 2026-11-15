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
from typing import Any

from pipeline.actor import bind_namespace, current_namespace
from pipeline.namespace_guard import audit_refusal, resolve_child_namespace
from pipeline.store_namespaces import namespaces_with_plans

logger = logging.getLogger("pipeline.scheduler_namespaces")


def _record_view(server: Any) -> Any | None:
    """Return the store's namespaced record view, or ``None`` when it has none."""
    records = getattr(getattr(server, "_store", None), "records", None)
    return records() if callable(records) else None


def _advance_namespace(server: Any, records: Any, namespace: str) -> tuple[dict, list[str]]:
    """Advance every plan stored in *namespace*; return results and refusal reasons.

    Must run with *namespace* bound: ``advance_pipeline`` reads and writes
    through the bound namespace.
    """
    plans: dict[str, dict] = {}
    refusals: list[str] = []
    for plan_name in records.list_plans(namespace):
        try:
            manifest = records.get_manifest(namespace, plan_name) or {}
            _, reason = resolve_child_namespace(manifest, namespace, namespace)
            if reason is not None:
                refusals.append(reason)
                plans[plan_name] = {"ok": False, "error": reason}
                continue
            plans[plan_name] = server.advance_pipeline(plan_name)
        except Exception as exc:  # noqa: BLE001 - reported by class name, one plan only
            plans[plan_name] = {"ok": False, "error": f"advance raised {type(exc).__name__}"}
    return plans, refusals


def advance_all_namespaces() -> dict[str, Any]:
    """Advance the default namespace, then every other namespace that holds plans."""
    from pipeline import server

    records = _record_view(server)
    if records is None:
        return server.advance_all_plans()

    try:
        others = [
            namespace
            for namespace in namespaces_with_plans(records)
            if namespace != current_namespace()
        ]
    except Exception as exc:  # noqa: BLE001 - enumeration failure is reported, default still runs
        logger.error("namespace enumeration failed error_class=%s", type(exc).__name__)
        result = server.advance_all_plans()
        result["namespace_error"] = "namespace_enumeration_failed"
        return result

    result = server.advance_all_plans()
    if not others:
        return result

    namespaces: dict[str, dict] = {}
    for namespace in others:
        with bind_namespace(namespace):
            plans, refusals = _advance_namespace(server, records, namespace)
        for reason in refusals:
            audit_refusal(reason)
        namespaces[namespace] = {"ok": True, "plans": plans}
    result["namespaces"] = namespaces
    return result
