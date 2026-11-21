"""The queue-mode reconcile sweep across every in-scope namespace (ADR 0012, nq-04).

``sweep_namespaces`` queues each active plan whose last advance has gone stale.
The default namespace is swept exactly as the pre-queue sweep always was. Every
other namespace the scheduler scope admits is swept through the store's record
view, one listing per namespace, and its requests carry ``namespace=``: a plan
name is unique only inside its namespace, so the same name may be queued once
per namespace.

The scope is read on every pass and applies as it does to the scheduler tick:
an invalid scope refuses the whole pass, and a scope that excludes the default
namespace leaves its plans unqueued.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from pipeline import plan_activity
from pipeline.actor import current_namespace
from pipeline.scheduler_namespaces import _record_view, _scoped_others
from pipeline.scheduler_scope import SchedulerScope, SchedulerScopeError, load_scope

logger = logging.getLogger(__name__)


def _default_plans(server) -> Iterable[tuple[str, dict | None]]:
    """Yield ``(plan, manifest)`` for the default namespace, reading lazily as today's loop did."""
    for plan_name in list(server._store.list_manifests()):
        yield plan_name, server._store.get_manifest_or_none(plan_name)


def _read_manifests(records, namespace: str) -> dict[str, dict | None]:
    """Return every manifest of *namespace*, in one call when the view offers ``get_manifests``."""
    get_manifests = getattr(records, "get_manifests", None)
    if callable(get_manifests):
        return get_manifests(namespace)
    return {
        plan_name: records.get_manifest(namespace, plan_name)
        for plan_name in records.list_plans(namespace)
    }


def _sweep_plans(queue, plans, now: float, stale_seconds: float, namespace=None) -> list[str]:
    """Request each active plan in *plans* whose last advance has gone stale; return those names.

    ``namespace`` is passed only when it is set, so the default namespace calls
    the queue exactly as it always has.
    """
    kwargs = {} if namespace is None else {"namespace": namespace}
    queued: list[str] = []
    for plan_name, manifest in plans:
        if plan_activity.is_inert(manifest):
            continue
        last_advanced = queue.last_advanced(plan_name, **kwargs)
        if last_advanced is not None and now - last_advanced <= stale_seconds:
            continue
        queue.request(plan_name, "sweep", **kwargs)
        queued.append(plan_name)
    return queued


def _sweep_other_namespaces(
    server, queue, scope: SchedulerScope, now: float, stale_seconds: float
) -> dict[str, list[str]]:
    """Sweep every in-scope namespace other than the default; return those that queued plans.

    A namespace that fails to list is logged by exception class and skipped;
    the rest still sweep. An enumeration failure logs and sweeps no others.
    """
    records = _record_view(server)
    if records is None:
        return {}
    try:
        others = _scoped_others(records, scope)
    except Exception as exc:  # noqa: BLE001 - enumeration failure is logged, the default still sweeps
        logger.error("namespace enumeration failed error_class=%s", type(exc).__name__)
        return {}

    swept: dict[str, list[str]] = {}
    for namespace in others:
        try:
            manifests = _read_manifests(records, namespace)
        except Exception as exc:  # noqa: BLE001 - one namespace is skipped, the others still sweep
            logger.error("namespace listing failed error_class=%s", type(exc).__name__)
            continue
        plans = _sweep_plans(queue, manifests.items(), now, stale_seconds, namespace)
        if plans:
            swept[namespace] = plans
    return swept


def sweep_namespaces(queue, pool, stale_seconds: float, now: float) -> dict:
    """Queue every stale, active plan in every in-scope namespace; never advance.

    ``stale_seconds`` is the age after which a plan is re-queued and ``now`` the
    clock ``queue.last_advanced`` stamps with. The result carries ``queued``
    (the default namespace's plan names) and ``namespaces`` (a plan list per
    other namespace that queued something). An invalid scope refuses the pass.
    """
    # Resolved here: importing pipeline.server at module load would close an import cycle.
    from pipeline import server

    try:
        scope = load_scope(os.environ) or SchedulerScope()
    except SchedulerScopeError as exc:
        logger.error("scheduler scope refused error_class=%s", type(exc).__name__)
        return {"ok": False, "queued": [], "namespace_error": "scheduler_scope_invalid"}

    queued: list[str] = []
    if scope.allows(current_namespace()):
        queued = _sweep_plans(queue, _default_plans(server), now, stale_seconds)
    swept = _sweep_other_namespaces(server, queue, scope, now, stale_seconds)

    result = {"ok": True, "queued": queued, "workers": pool.stats()}
    if swept:
        result["namespaces"] = swept
    return result
