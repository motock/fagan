"""Record-store calls in the namespace bound for the current call.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so every manifest, journal and decision read or write below a scheduler tick
must select that namespace instead of naming the default partition. These
wrappers are the one place that selection happens; a caller with no binding
gets ``current_namespace()``'s default, so single-namespace installs and the
background paths are unchanged.
"""

from __future__ import annotations

from typing import Any

from .actor import current_namespace
from .store_records import DEFAULT_NAMESPACE


def get_manifest(store: Any, plan_name: str) -> dict | None:
    """Return the plan's manifest in the bound namespace, or None."""
    return store.records().get_manifest(current_namespace(), plan_name)


def put_manifest(store: Any, plan_name: str, manifest: dict) -> None:
    """Write the plan's manifest in the bound namespace."""
    store.records().put_manifest(current_namespace(), plan_name, manifest)


def get_journal(store: Any, plan_name: str, story_key: str):
    """Return the story journal in the bound namespace."""
    return store.records().get_journal(current_namespace(), plan_name, story_key)


def append_journal(store: Any, plan_name: str, story_key: str, record: dict) -> None:
    """Append one journal record in the bound namespace."""
    store.records().append_journal(current_namespace(), plan_name, story_key, record)


def append_decision(store: Any, plan_name: str, record: dict) -> None:
    """Append one decision record in the bound namespace."""
    store.records().append_decision(current_namespace(), plan_name, record)


def get_decisions(store: Any, plan_name: str) -> list[dict]:
    """Return the plan's decision records in the bound namespace."""
    return store.records().get_decisions(current_namespace(), plan_name)


def list_plans(store: Any) -> list[str]:
    """Return the plan names held in the bound namespace."""
    return store.records().list_plans(current_namespace())


def refuses_bound_namespace(store: Any) -> bool:
    """Whether *store* holds only the default partition end to end.

    The file layout's audit log refuses every non-default namespace, so a
    caller that must return a refusal rather than raise asks this first. A
    record store (plugin, SQLite, in-memory) selects the namespace per call,
    and nothing bound to the default namespace is ever refused.
    """
    if current_namespace() == DEFAULT_NAMESPACE:
        return False
    from .store import FileRecords

    return isinstance(store.records(), FileRecords)
