"""Plan-approval state and content digest for ingested plans (ENT-LC-01).

The approval gate is opt-in and off by default: with ``PIPELINE_PLAN_APPROVAL``
unset (or ``off``/``0``/``false``/empty) every manifest is written exactly as
it was before this module existed — no ``approval`` key is added, modified or
removed.

When the gate is enabled, ingest records an ``approval`` block on the manifest:

* ``pending_approval`` — the authored content has not been approved (yet).
* ``approved`` — a human/agent approved *this exact* authored content.

The record carries a ``digest`` of the manifest's authored content so an
approval can be tied to the content it approved. ``canonical_digest`` hashes
only authored fields (never the ``approval`` block itself, never runtime
state), so recording a decision cannot invalidate the digest it was made
against. ``on_ingest`` recomputes the digest and keeps ``approved`` only when
the previous record was approved *and* its digest still matches; any authored
change therefore resets the record to ``pending_approval`` while preserving the
previous decision metadata for auditability.

``reset_if_changed`` applies the same digest rule to edits made through the
patch tools (``patch_story`` / ``patch_plan``): an approval cannot outlive an
authored change made through those tools. It is not a blanket guard for every
writer of authored fields: ``save_plan`` is covered by the source-digest guard
at the next ingest, while ``set_plan_role_config`` writes ``role_config``
directly and is *not* reset here — an approval can outlive such a change until
the next ingest. Closing that window is tracked with the enforcement story.
"""
import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from app.identity import Principal

from .actor import current_correlation_id, current_principal

APPROVAL_ENV = "PIPELINE_PLAN_APPROVAL"

#: Authored, plan-level fields that participate in the content digest.
_PLAN_AUTHORED_FIELDS = ("repo_root", "role_config", "context")

#: Authored, per-story fields that participate in the content digest. Runtime
#: state (status, attempts, sizing_auto_routed, ...) is deliberately excluded:
#: it changes as the pipeline runs and must not invalidate an approval.
_STORY_AUTHORED_FIELDS = (
    "summary",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
    "files",
    "blocked_by",
)

_TRUE_VALUES = frozenset({"on", "1", "true"})
_FALSE_VALUES = frozenset({"", "off", "0", "false"})

#: Previous decision metadata carried forward across re-ingests for audit.
#: ``at`` is the decision timestamp written by ``PlanApprovalMixin._decide_plan``
#: (``datetime.now(UTC).isoformat()``); ``decided_at`` is accepted as a legacy
#: alias so records written by older builds keep their audit trail.
_PRESERVED_DECISION_FIELDS = (
    "decided_by",
    "at",
    "decided_at",
    "correlation_id",
    "role",
    "self_approved",
)

#: Decision metadata cleared when the authored content changes. ``reason`` is
#: written by a ``changes_requested`` decision and must not outlive it.
_CLEARED_DECISION_FIELDS = _PRESERVED_DECISION_FIELDS + ("reason",)


def approval_enabled() -> bool:
    """Return whether the plan-approval gate is enabled.

    Read on every call (never cached) so a long-lived process honours a
    changed environment. Unrecognised values raise ``ValueError`` naming both
    the variable and the offending value: a typo must fail closed rather than
    silently disabling (or enabling) the gate.
    """
    raw = os.environ.get(APPROVAL_ENV)
    if raw is None:
        return False
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{APPROVAL_ENV} must be one of "
        f"{sorted(_TRUE_VALUES | _FALSE_VALUES)} (or unset); got {raw!r}"
    )


def canonical_digest(manifest: dict[str, Any]) -> str:
    """Return the sha256 digest of ``manifest``'s authored content.

    Only authored content is hashed: the plan-level ``repo_root``,
    ``role_config`` and ``context``, plus each story's authored fields. The
    ``approval`` block and runtime story state are excluded so that recording
    or changing a decision never changes the digest. The manifest is not
    mutated.
    """
    payload: dict[str, Any] = {
        field: manifest.get(field) for field in _PLAN_AUTHORED_FIELDS
    }
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        stories = {}
    payload["stories"] = {
        key: {field: story.get(field) for field in _STORY_AUTHORED_FIELDS}
        for key, story in stories.items()
        if isinstance(story, dict)
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_activatable(manifest: dict[str, Any]) -> bool:
    """Return whether ``manifest`` may be activated (dispatched).

    Always true while the gate is disabled — checked first, so a malformed
    ``approval`` value cannot break the default-off path. When enabled, only an
    ``approval`` dict whose ``status`` is exactly ``"approved"`` qualifies.
    """
    if not approval_enabled():
        return True
    approval = manifest.get("approval")
    if not isinstance(approval, dict):
        return False
    return approval.get("status") == "approved"


def on_ingest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the approval record to persist for ``manifest`` at ingest time.

    The digest is recomputed from the authored content and compared against the
    previous record *before* anything is assigned, so a content change always
    resets the status to ``pending_approval``. Previous decision metadata
    (``decided_by``, ``at``, ``role``, ``self_approved``, ``warnings_seen``) is
    preserved for auditability. The input manifest is not mutated.
    """
    previous = manifest.get("approval")
    if not isinstance(previous, dict):
        previous = {}

    digest = canonical_digest(manifest)
    keep_approved = (
        previous.get("status") == "approved" and previous.get("digest") == digest
    )

    warnings_seen = previous.get("warnings_seen")
    if not isinstance(warnings_seen, list):
        warnings_seen = []

    record: dict[str, Any] = {
        "status": "approved" if keep_approved else "pending_approval",
        "digest": digest,
        "warnings_seen": list(warnings_seen),
    }
    for field in _PRESERVED_DECISION_FIELDS:
        if field in previous:
            record[field] = previous[field]
    return record


def reset_if_changed(manifest: dict[str, Any]) -> bool:
    """Reset ``manifest``'s approval record when its authored content changed.

    Compares ``canonical_digest(manifest)`` with the digest stored on the
    ``approval`` record. When they differ the record is reset to
    ``pending_approval``, the decision metadata (``decided_by``, ``at``,
    ``role``, ``self_approved``, plus a ``changes_requested`` ``reason``) is
    cleared and the digest is updated; the function returns ``True``. A
    matching digest returns ``False`` and changes nothing.

    While the gate is disabled this is a no-op that returns ``False`` without
    touching the manifest, so the default-off path stays byte-identical to
    before this function existed. Non-decision metadata (``warnings_seen``) is
    preserved for auditability, mirroring ``on_ingest``.
    """
    if not approval_enabled():
        return False

    digest = canonical_digest(manifest)
    previous = manifest.get("approval")
    if not isinstance(previous, dict):
        previous = {}
    if previous.get("digest") == digest:
        return False

    record = {**previous, "status": "pending_approval", "digest": digest}
    for field in _CLEARED_DECISION_FIELDS:
        record.pop(field, None)
    manifest["approval"] = record
    return True


#: Order in which a principal's ADR 0009 roles are reported on a decision
#: record: the most privileged role held wins, so the record names the
#: authority the decision was taken with rather than a member of an unordered set.
_ROLE_PRECEDENCE = ("admin", "owner", "approver", "delegate", "author")


def _primary_role(principal: Principal) -> str:
    """Return the most privileged ADR 0009 role *principal* holds."""
    for role in _ROLE_PRECEDENCE:
        if role in principal.roles:
            return role
    return "operator"


#: Refusal message when the authored content no longer matches the digest the
#: approval record was made against.
_DIGEST_MISMATCH_ERROR = "plan content changed; re-ingest before approving"


class PlanApprovalMixin:
    """Approval transitions for ``PipelineService`` (ENT-LC-02a).

    Mixed into ``pipeline.service.PipelineService`` so the transport-agnostic
    seam owns the transitions while ``pipeline/server.py`` keeps its one-line
    ``@mcp.tool()`` delegations. The mixin holds no state: every call reads the
    live ``pipeline.server`` bindings, which is why ``_store`` and
    ``_append_decision`` are imported *inside* the methods — ``pipeline.server``
    imports ``pipeline.service``, so a module-level import would cycle.

    Both transitions are no-ops while ``PIPELINE_PLAN_APPROVAL`` is off: they
    return ``{"ok": True, "skipped": "approval_disabled"}`` without reading or
    writing the manifest, so the default-off path is byte-identical to before
    this mixin existed.
    """

    def approve_plan(self, plan_name: str) -> dict[str, Any]:
        """Approve ``plan_name``'s authored content (``pending_approval`` ->
        ``approved``)."""
        return self._decide_plan(plan_name, status="approved", reason="")

    def reject_plan(self, plan_name: str, reason: str = "") -> dict[str, Any]:
        """Request changes on ``plan_name`` (``pending_approval`` ->
        ``changes_requested``), recording ``reason`` when given."""
        return self._decide_plan(plan_name, status="changes_requested", reason=reason)

    def _decide_plan(
        self, plan_name: str, *, status: str, reason: str
    ) -> dict[str, Any]:
        """Apply one approval decision and append it to the decisions log.

        Refuses (``ok=False``) when the recomputed canonical digest differs
        from the digest stored on the approval record: the operator would
        otherwise be approving content they never saw.
        """
        from .parsers import _validate_key
        from .server import _append_decision, _store

        _validate_key(plan_name)
        if not approval_enabled():
            return {"ok": True, "skipped": "approval_disabled"}

        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {"ok": True, "skipped": "locked"}
            if not _store.manifest_path(plan_name).exists():
                return {"ok": False, "error": f"No such plan {plan_name!r}"}
            manifest = _store.get_manifest(plan_name)
            digest = canonical_digest(manifest)
            previous = manifest.get("approval")
            if not isinstance(previous, dict):
                previous = {}
            if previous.get("digest") != digest:
                return {"ok": False, "error": _DIGEST_MISMATCH_ERROR}

            warnings_seen = previous.get("warnings_seen")
            if not isinstance(warnings_seen, list):
                warnings_seen = []
            principal = current_principal()
            ingested_by = manifest.get("ingested_by") or principal.subject
            record: dict[str, Any] = {
                "plan": plan_name,
                "status": status,
                "digest": digest,
                "decided_by": principal.subject,
                "at": datetime.now(UTC).isoformat(),
                "correlation_id": current_correlation_id(),
                "role": _primary_role(principal),
                "self_approved": principal.subject == ingested_by,
                "warnings_seen": list(warnings_seen),
            }
            if reason:
                record["reason"] = reason

            manifest["approval"] = {**previous, **record}
            _store.save_manifest(plan_name, manifest)
            _append_decision(plan_name, record)
            return {"ok": True, **record}
