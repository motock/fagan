"""Read a staged plan draft through the record store (ADR 0012 decision 3).

``save_plan`` stages a plan draft with ``records.put_plan_draft``; ingest reads
it back here, so neither the write nor the read composes a ``PLAN_DIR`` path.
The refusals are the ones :func:`pipeline.persistence.read_plan_file` has always
answered with -- ``store_records.get_plan_draft`` carries that reader's own
reason text on :class:`pipeline.store_records.UnreadableRecordError` -- so
ingest's messages stay byte-identical while the reader moves to the contract.
"""

from __future__ import annotations

from typing import Any

from pipeline.store_records import DEFAULT_NAMESPACE, UnreadableRecordError


def read_plan_draft(plan_name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(plan, refusal)`` for ``plan_name``'s staged draft.

    A draft that is missing, unreadable, malformed, or not a JSON object is a
    structured refusal rather than an exception, exactly as
    ``read_plan_file`` answers; only a plan staged through the record contract
    is returned.  ``pipeline.server._store`` is resolved per call, so a store
    or plans directory patched on that module is the one read.
    """
    from pipeline import server

    try:
        plan = server._store.records().get_plan_draft(DEFAULT_NAMESPACE, plan_name)
    except UnreadableRecordError as exc:
        return None, {'ok': False, 'error': str(exc)}
    if plan is None:
        return None, {'ok': False, 'error': f'No plan named {plan_name}'}
    return plan, None


def list_plan_names(records: Any, namespace: str = DEFAULT_NAMESPACE) -> list[str]:
    """Return the sorted plan names with a staged draft *or* a manifest.

    A plan is named once, however many files back it: the draft listing
    contributes names and the manifest listing contributes names, and the union
    is what the MCP ``list_plans`` tool has always meant.  Sidecars never take
    part -- ``list_plan_drafts`` admits only ``<plan>.json`` documents holding
    an ``epics`` key, so ``<plan>.decisions`` and journals cannot surface.
    """
    drafted = records.list_plan_drafts(namespace)
    return sorted(set(drafted) | set(records.list_plans(namespace)))


def list_ingestable_plan_names(records: Any, namespace: str = DEFAULT_NAMESPACE) -> list[str]:
    """Return the sorted plan names with a draft and no manifest yet.

    A plan that already has a manifest has been ingested and is no longer an
    ingest target; one whose draft is malformed is skipped by
    ``list_plan_drafts`` rather than raised, so one bad draft cannot blind a
    caller that lists them all.
    """
    ingested = set(records.list_plans(namespace))
    return [name for name in records.list_plan_drafts(namespace) if name not in ingested]
