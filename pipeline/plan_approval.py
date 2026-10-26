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
"""
import hashlib
import json
import os
from typing import Any

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
_PRESERVED_DECISION_FIELDS = (
    "decided_by",
    "decided_at",
    "role",
    "self_approved",
)


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
    (``decided_by``, ``decided_at``, ``role``, ``self_approved``,
    ``warnings_seen``) is preserved for auditability. The input manifest is not
    mutated.
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
