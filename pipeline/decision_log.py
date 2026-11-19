"""The one seam every ruling and human resolution goes through.

Decisions - the ``failure triage`` ruling, the ``failure triage execution``
record and the ``human resolution`` - used to be written straight to
``<plan>.decisions.json`` on this host. A deployment whose store owns its
decision storage (ADR 0012 stage B) keeps them in the store instead, so the
writers and readers here all pick the same stream: the store's namespaced
record view when the configured store has one, the legacy file otherwise.

Writers delegate to :func:`pipeline.merge._append_adjudication_decision`, the
store-first writer the ``high-risk merge adjudication`` records already use, so
there is exactly one implementation of the store-first choice. The import is
lazy: :mod:`pipeline.merge` imports a large part of the pipeline and importing
it at module load time would close an import cycle.

Readers mirror that choice exactly, so a reader never sees a different stream
than the writer wrote.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

__all__ = ["append_decision", "read_decisions"]


def append_decision(plan_name: str, record: dict) -> None:
    """Append one decision record for ``plan_name`` through the store-first writer."""
    from .merge import _append_adjudication_decision

    _append_adjudication_decision(plan_name, record)


def _configured_store():
    """Return the configured store, or ``None`` when no store is configured."""
    from . import server

    return getattr(server, "_store", None)


def _records_view(store):
    """Return the store's namespaced record view, or ``None`` when it has none."""
    if store is None:
        return None
    records = getattr(store, "records", None)
    if not callable(records):
        return None
    return records()


def _read_legacy(plan_name: str) -> list[dict]:
    """Read ``<plan>.decisions.json`` exactly as the triage outcomes always have."""
    from .persistence import _decisions_path

    path = _decisions_path(plan_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        log.warning(
            "decision log unreadable",
            extra={"event": "decisions_unreadable", "plan_name": plan_name},
        )
        return []
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict)]


def store_owns_records() -> bool:
    """True when the configured store keeps decisions in its own record view.

    The file store keeps the legacy ``<plan>.decisions.json`` writer, and so
    does a store with no record view at all, so both report ``False``.
    """
    from .store import FileStore

    store = _configured_store()
    if isinstance(store, FileStore):
        return False
    return _records_view(store) is not None


def read_decisions(plan_name: str) -> list[dict]:
    """Return the decision records for ``plan_name`` from the writer's stream."""
    store = _configured_store()
    if not store_owns_records():
        return _read_legacy(plan_name)

    from . import bound_records

    decisions = bound_records.get_decisions(store, plan_name) or []
    return [record for record in decisions if isinstance(record, dict)]
