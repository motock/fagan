"""Read a staged plan draft through the record store (ADR 0012 decision 3).

``save_plan`` stages a plan draft with ``records.put_plan_draft``; ingest reads
it back here, so neither the write nor the read composes a plans-directory path.
The refusals are the ones :func:`pipeline.persistence.read_plan_file` has always
answered with -- ``store_records.get_plan_draft`` carries that reader's own
reason text on :class:`pipeline.store_records.UnreadableRecordError` -- so
ingest's messages stay byte-identical while the reader moves to the contract.
"""

from __future__ import annotations

from typing import Any

from pipeline.store_records import DEFAULT_NAMESPACE, UnreadableRecordError


def _records() -> Any:
    """Return the live store's record view, or ``None`` when it has none.

    The store is resolved per call on ``pipeline.server``, so a store or plans
    directory patched there is the one read.  A partial store -- a test double
    or a delegating wrapper that only carries the legacy file API -- has no
    record view, and the draft is then read through its own ``manifest_path``,
    which is the plans directory the record view would have used anyway.
    """
    from pipeline import server

    store = server._store
    records = getattr(store, 'records', None)
    return records() if records is not None else None


def _read_plan_draft(plan_name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(plan, refusal)`` for ``plan_name``'s staged draft.

    A draft that is missing, unreadable, malformed, or not a JSON object is a
    structured refusal rather than an exception, exactly as
    ``read_plan_file`` answers; only a plan staged through the record contract
    is returned.
    """
    records = _records()
    if records is None:
        from pipeline import persistence, server

        path = server._store.manifest_path(plan_name).parent / f'{plan_name}.json'
        return persistence.read_plan_file(path)
    try:
        plan = records.get_plan_draft(DEFAULT_NAMESPACE, plan_name)
    except UnreadableRecordError as exc:
        return None, {'ok': False, 'error': str(exc)}
    if plan is None:
        return None, {'ok': False, 'error': f'No plan named {plan_name}'}
    return plan, None


def read_plan_draft(plan_name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """:func:`_read_plan_draft`, naming the way out when a full ingest consumed the draft.

    A plan that has a manifest but no draft was ingested and its draft consumed
    (ADR 0012 decision 5), so re-ingesting needs a fresh ``save_plan``. A plan
    with neither keeps the legacy ``No plan named`` refusal.
    """
    plan, refusal = _read_plan_draft(plan_name)
    if plan is not None or refusal is None or refusal.get('error') != f'No plan named {plan_name}':
        return plan, refusal
    from pipeline import server

    if not server._store.manifest_path(plan_name).exists():
        return plan, refusal
    return None, {
        'ok': False,
        'error': (
            f"plan '{plan_name}' has no staged draft: save_plan it "
            '(export_plan renders the current plan) before re-ingesting'
        ),
    }


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
