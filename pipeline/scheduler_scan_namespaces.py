"""The scheduler tick's event scan across namespaces.

The default namespace is scanned exactly as the daemon always scanned it, by the
``scan_default`` the caller passes in. Each other in-scope namespace that holds
plans is then scanned with :func:`pipeline.watchers.scan_all_plans` inside
``bind_namespace``, so its manifests, done markers and wake markers are its own.

The namespaces are enumerated and filtered exactly as
:mod:`pipeline.scheduler_namespaces` advances them: ``PIPELINE_SCHEDULER_NAMESPACES_ONLY``
and ``..._SKIP`` (see :mod:`pipeline.scheduler_scope`), with the default
namespace scanned only when the scope admits it. An invalid scope runs only
``scan_default`` and logs one WARNING.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from pipeline import watchers
from pipeline.actor import bind_namespace, current_namespace
from pipeline.scheduler_namespaces import _record_view, _scoped_others
from pipeline.scheduler_scope import SchedulerScope, SchedulerScopeError, load_scope

logger = logging.getLogger("pipeline.scheduler_scan_namespaces")


def _in_scope_others(scope: SchedulerScope) -> list[str]:
    """Return the non-default namespaces *scope* admits, or [] without a record view."""
    from pipeline import server

    records = _record_view(server)
    if records is None:
        return []
    return _scoped_others(records, scope)


def scan_all_namespaces(
    scan_default: Callable[[], list[dict[str, Any]]], bus: Any
) -> list[dict[str, Any]]:
    """Scan the default namespace via *scan_default*, then every other in-scope namespace.

    *scan_default* runs unguarded and first, so a default-namespace failure
    propagates exactly as it did before namespaces were scanned. Each other
    namespace runs ``watchers.scan_all_plans(bus)`` under its own binding; a
    failure there is logged by exception class and the next namespace still runs.

    Returns the default scan's events followed by each namespace's events.
    """
    try:
        scope = load_scope(os.environ)
    except SchedulerScopeError as exc:
        logger.warning("scheduler scope refused error_class=%s", type(exc).__name__)
        return list(scan_default() or [])

    # No scope variable set means every namespace is in scope.
    scope = scope or SchedulerScope()
    events: list[dict[str, Any]] = []
    if scope.allows(current_namespace()):
        events.extend(scan_default() or [])

    try:
        others = _in_scope_others(scope)
    except Exception as exc:  # noqa: BLE001 - enumeration failure is reported, default still ran
        logger.warning("namespace enumeration failed error_class=%s", type(exc).__name__)
        return events

    for namespace in others:
        try:
            with bind_namespace(namespace):
                events.extend(watchers.scan_all_plans(bus) or [])
        except Exception as exc:  # noqa: BLE001 - reported by class name, one namespace only
            logger.warning("namespace scan failed error_class=%s", type(exc).__name__)
    return events
