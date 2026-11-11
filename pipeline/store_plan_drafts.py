"""The transient plan-draft record operations of the file record view.

A *plan draft* is the staged plan document an operator hands the pipeline
before ingest (ADR 0012 decision 3, "Plan draft (transient) ``<plan>.json``").
It is not durable store state: it is the plan JSON ``save_plan`` writes and
ingest reads, so the file store keeps today's layout and the many callers that
write a plan file by hand keep working.

The operations live here, as a mixin, for the same reason the outbox contract
does (``pipeline/store_contract_outbox.py``): ``pipeline/store.py`` sits on the
1000-line gate, so a record kind with real behaviour arrives as its own module
and the record view only inherits it.

The host class supplies ``_store`` (the legacy file store, whose
``manifest_path`` resolves the plans directory per call) and
``_refuse_other_namespace`` (this view serves ``DEFAULT_NAMESPACE`` only).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.store_records import (
    UnreadableRecordError,
    validate_namespace,
    validate_plan_name,
)


class PlanDraftRecords:
    """The four plan-draft operations, mixed into the file record view."""

    #: Provided by the host record view.
    _store: Any

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _draft_path(self, plan_name: str) -> Path:
        """Return ``<plans dir>/<plan>.json``, resolved per call.

        The plans directory comes from the file store's own ``manifest_path``,
        which reads ``pipeline.server.PLAN_DIR`` through a :class:`LiveRef` at
        call time, so a plans directory patched on that module is the one the
        draft lands in.
        """
        return self._store.manifest_path(plan_name).parent / f"{plan_name}.json"

    def put_plan_draft(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Stage ``plan`` as ``plan_name``'s transient plan draft.

        The layout is the one ingest already reads -- ``<plan>.json`` in the
        plans directory -- so a plan handed over by hand and a plan handed over
        by ``save_plan`` land in the same place, and the draft replaces any
        previous one wholesale.

        The write goes through ``pipeline.server._atomic_write_json`` resolved
        per call (the module attribute, never an import-time copy), so a patched
        writer is the one the write goes through and ``save_plan``'s staging and
        this call share a single writer.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        if not isinstance(plan, dict):
            raise TypeError('plan must be a dict')
        # Serialise before touching storage: a document that cannot be stored
        # must leave no trace behind, not even a half-written draft.
        json.dumps(plan)
        from pipeline import server

        server._atomic_write_json(self._draft_path(plan_name), plan)

    def get_plan_draft(self, namespace: str, plan_name: str) -> dict | None:
        """Return ``plan_name``'s plan draft, or ``None`` when it has none.

        Read through :func:`pipeline.persistence.read_plan_file`, the reader
        ingest already refuses with, so a malformed or unreadable draft raises
        :class:`UnreadableRecordError` carrying that reader's own reason text
        and ingest's refusal messages stay byte-identical.  Missing is not
        unreadable: an absent draft is ``None``.  Nothing is cached, so a draft
        rewritten on disk is read afresh on the next call.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        path = self._draft_path(plan_name)
        if not path.is_file():
            return None
        from pipeline import persistence

        plan, refusal = persistence.read_plan_file(path)
        if refusal is not None:
            raise UnreadableRecordError(refusal['error'])
        return plan

    def delete_plan_draft(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s draft; report whether one was deleted.

        ``False`` -- not an error -- when there was nothing to delete, so the
        call is idempotent and a second delete reports ``False``.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        try:
            self._draft_path(plan_name).unlink()
        except FileNotFoundError:
            return False
        return True

    def list_plan_drafts(self, namespace: str) -> list[str]:
        """Return the sorted plan names that have an ingestable draft.

        Only ``<plan>.json`` files count: a stem holding a ``'.'`` is a
        manifest, decisions or journal sidecar, and the file must hold a JSON
        object with an ``epics`` key -- the same filter
        ``PipelineService.list_ingestable_plans`` applies.  A file that cannot
        be read or parsed is skipped rather than raised: one bad draft must not
        blind a caller that lists them all.
        """
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        drafts = []
        for path in self._draft_path('draft').parent.glob('*.json'):
            if '.' in path.stem:
                continue
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and 'epics' in document:
                drafts.append(path.stem)
        return sorted(drafts)
