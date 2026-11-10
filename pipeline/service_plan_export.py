"""The ``PipelineService.export_plan`` read tool, as a mixin.

It lives here rather than in pipeline/service.py for the same reason
``service_decisions``/``service_role_config``/``service_story_ops`` do: that
module is at its 1000-line cap (scripts/check_line_limit.py), so a new method
body goes in its own module and only the mixin joins the class.

The store and the plan-name validator resolve through pipeline.server at call
time, exactly like the service's own ``_ServerRef`` bindings, so tests that
patch ``pipeline.server._store`` keep landing here.
"""

from __future__ import annotations

from typing import Any

from .module_ref import _ModuleRef
from .plan_export import render_plan_document

_store = _ModuleRef("pipeline.server", "_store")
_validate_key = _ModuleRef("pipeline.server", "_validate_key")


class PlanExportMixin:
    """Mixed into ``PipelineService``: read a plan back as a plan document."""

    def export_plan(self, plan_name: str) -> dict[str, Any]:
        """Render *plan_name*'s plan document from its ingested manifest.

        The document is rendered on demand (ADR 0012 decision 5): the manifest
        is the plan record, so the ``save_plan``-shaped document is derived
        from it rather than stored a second time and left to drift. Runtime
        state never reaches the export; see ``pipeline.plan_export`` for what
        is kept and dropped, and why the round trip matters for plan approval
        (ADR 0007).

        The plan name is validated before the manifest is read, so an invalid
        name is a validation refusal and never a store lookup.

        Args:
            plan_name: the plan's name, as passed to
                ``save_plan``/``ingest_plan`` and as returned by ``list_plans``.

        Returns:
            ``{'ok': True, 'plan': <document>}``, or ``{'ok': False, 'error':
            'Plan not found'}`` for a plan that was never ingested.
        """
        _validate_key(plan_name)
        manifest = _store.records().get_manifest("default", plan_name)
        if manifest is None:
            return {"ok": False, "error": "Plan not found"}
        return {"ok": True, "plan": render_plan_document(manifest)}
