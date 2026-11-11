"""One-time repair of approvals recorded under a superseded digest version.

Bumping ``APPROVAL_DIGEST_VERSION`` (``pipeline.plan_approval``) invalidates
every stored approval on purpose: an approval made under an older version was
made against a digest that covered *different* content, so it can never stay
silently in force. Such a record cannot activate the plan and cannot be
decided on either (``_decide_plan`` refuses it), which would strand the plan.

This command is the recovery path. It walks every plan whose manifest exists,
recomputes the canonical digest of the content on disk now, and:

* ``reset_to_pending`` — an ``approved`` record (or a malformed one, or one
  with an unknown status: fail closed) becomes ``pending_approval`` with the
  current digest and version, its decision metadata cleared exactly as
  ``reset_if_changed`` clears it, ``warnings_seen`` kept and
  ``reset_reason: "digest_version_upgrade"`` recorded. One
  ``plan_pending_approval`` notice follows.
* ``restamped`` — a ``pending_approval`` or ``changes_requested`` record keeps
  its status (and a ``changes_requested`` reason: the request still stands and
  the plan stays non-activatable) and is rebound to the current digest and
  version.
* ``current`` — a record written under the current version is untouched.
* ``no_record`` — a plan without an ``approval`` dict is untouched, so the
  gate-off path stays byte-identical.
* ``skipped_locked`` / ``skipped_unreadable`` — the plan was left exactly as
  it was (no write, no audit, no notice).

**It never approves a plan.** Every record this command writes carries a
status other than ``approved``, so the next ``approve_plan`` (or dashboard
approve) is a deliberate decision on content the reviewer is shown.

Every write appends one ``plan.approval_reset`` audit event and nothing more:
no payloads, no principals beyond the actor the audit module stamps. Running
it twice is safe — the second run reports ``current`` for everything.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import plan_approval
from ..concurrency import command_lock_wait_seconds

#: The record was (re)set to ``pending_approval`` and needs a person again.
RESET_TO_PENDING = "reset_to_pending"
#: The record kept its status and was rebound to the current digest.
RESTAMPED = "restamped"
#: The record was already written under the current digest version.
CURRENT = "current"
#: The plan has no approval record at all.
NO_RECORD = "no_record"
#: The plan lock was held; nothing was read or written.
SKIPPED_LOCKED = "skipped_locked"
#: The manifest could not be read; nothing was written.
SKIPPED_UNREADABLE = "skipped_unreadable"

#: Outcomes that are not skips, in summary order.
_SUMMARY_OUTCOMES = (RESET_TO_PENDING, RESTAMPED, CURRENT, NO_RECORD)

#: Statuses that are rebound rather than reset: the operator's decision (a
#: pending plan, or a request for changes that still stands) survives.
_RESTAMP_STATUSES = frozenset({"pending_approval", "changes_requested"})

#: Why a record was returned to ``pending_approval``. Recorded on the record
#: so the reviewer sees that the reset was a version upgrade, not an edit.
RESET_REASON = "digest_version_upgrade"

#: The one audit action this command writes.
AUDIT_ACTION = "plan.approval_reset"

#: The notice event a reset plan raises.
PENDING_EVENT = "plan_pending_approval"


def _store() -> Any:
    """Return the live store (resolved per call: ``_store`` is built at import)."""
    from .. import server

    return server._store


def _plan_names(store: Any) -> list[str]:
    """Return the names of every plan whose manifest exists, sorted.

    Prefers the store's record-level manifest listing (``list_manifests``,
    store stage B1b); falls back to the plan listing filtered to plans whose
    manifest file exists.
    """
    lister = getattr(store, "list_manifests", None)
    if callable(lister):
        names = list(lister())
    else:
        recorded = set(store.records().list_plans("default"))
        names = [name for name in store.list_plans() if name in recorded]
    return sorted(set(names))


def _from_version(previous: dict[str, Any]) -> Any:
    """Return the stored digest version, or ``None`` when the field is absent."""
    return previous.get("digest_version")


def _pending_record(previous: dict[str, Any], digest: str) -> dict[str, Any]:
    """Return the ``pending_approval`` record for ``previous``.

    The decision metadata is cleared exactly as ``reset_if_changed`` clears it
    (``_CLEARED_DECISION_FIELDS``) and ``warnings_seen`` is kept, so the
    reviewer's earlier warnings still stand while the decision does not.
    """
    record = {
        key: value
        for key, value in previous.items()
        if key not in plan_approval._CLEARED_DECISION_FIELDS
    }
    warnings_seen = record.get("warnings_seen")
    if not isinstance(warnings_seen, list):
        warnings_seen = []
    record["status"] = "pending_approval"
    record["digest"] = digest
    record["digest_version"] = plan_approval.APPROVAL_DIGEST_VERSION
    record["warnings_seen"] = list(warnings_seen)
    record["reset_reason"] = RESET_REASON
    return record


def _restamped_record(previous: dict[str, Any], digest: str) -> dict[str, Any]:
    """Return ``previous`` rebound to ``digest`` with its status untouched."""
    return {
        **previous,
        "digest": digest,
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
    }


def _decide(manifest: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Return ``(outcome, new_approval_record_or_None)`` for ``manifest``.

    ``None`` for the record means "leave the manifest alone". The status the
    caller must never see produced here is ``approved``: a record under the
    current version is only ever *reported*, never written.
    """
    approval = manifest.get("approval")
    if approval is None:
        return NO_RECORD, None
    previous = approval if isinstance(approval, dict) else {}
    if isinstance(approval, dict) and plan_approval.digest_version_is_current(
        approval
    ):
        return CURRENT, None

    digest = plan_approval.canonical_digest(manifest)
    if previous.get("status") in _RESTAMP_STATUSES and isinstance(approval, dict):
        return RESTAMPED, _restamped_record(previous, digest)
    # ``approved`` (the documented case), any other status, and a malformed
    # record: fail closed and make a person decide again.
    return RESET_TO_PENDING, _pending_record(previous, digest)


def _apply(
    store: Any,
    plan_name: str,
    manifest: dict[str, Any],
    outcome: str,
    new_record: dict[str, Any],
) -> None:
    """Apply one outcome to ``manifest``: write it, audit it, notify.

    The audit detail carries the version the record was written under, the
    version it now carries, the status it had and the outcome -- and nothing
    else: no content, no principal beyond the actor the audit module stamps.
    """
    from ..audit import record_audit
    from ..plan_approval_notice import notify_approval_event

    approval = manifest.get("approval")
    previous = approval if isinstance(approval, dict) else {}
    detail = {
        "from_version": _from_version(previous),
        "to_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "previous_status": previous.get("status"),
        "outcome": outcome,
    }
    manifest["approval"] = new_record
    store.save_manifest(plan_name, manifest)
    record_audit(
        plan_name,
        action=AUDIT_ACTION,
        outcome="allowed",
        target={"plan": plan_name},
        detail=detail,
    )
    if outcome == RESET_TO_PENDING:
        notify_approval_event(plan_name, manifest, PENDING_EVENT)


def _reset_one(store: Any, plan_name: str, *, dry_run: bool) -> str:
    """Return the outcome for one plan, writing only when not ``dry_run``."""
    wait_seconds = command_lock_wait_seconds()
    with store.transaction(plan_name, wait_seconds) as acquired:
        if not acquired:
            return SKIPPED_LOCKED
        try:
            manifest = store.get_manifest(plan_name)
        except (OSError, ValueError):
            return SKIPPED_UNREADABLE
        if not isinstance(manifest, dict):
            return SKIPPED_UNREADABLE
        outcome, new_record = _decide(manifest)
        if new_record is None or dry_run:
            return outcome
        _apply(store, plan_name, manifest, outcome, new_record)
        return outcome


def run(*, dry_run: bool = False) -> dict[str, Any]:
    """Reset every outdated approval; return the outcomes and their summary.

    ``dry_run`` reports exactly the same outcomes and writes, audits and
    notifies nothing — the same read-only decision is computed and discarded.
    """
    store = _store()
    plans = [
        {"plan": plan_name, "outcome": _reset_one(store, plan_name, dry_run=dry_run)}
        for plan_name in _plan_names(store)
    ]
    summary = {outcome: 0 for outcome in _SUMMARY_OUTCOMES}
    summary["skipped"] = 0
    for entry in plans:
        if entry["outcome"] in summary:
            summary[entry["outcome"]] += 1
        else:
            summary["skipped"] += 1
    return {"plans": plans, "summary": summary, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one JSON line per plan, then the summary.

    Returns 0 when nothing was skipped and 1 otherwise, so a scheduler or an
    operator sees that some plans still need another run.
    """
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.tools.reset_outdated_approvals",
        description=(
            "Return every plan approval recorded under a superseded digest "
            "version to pending_approval (or restamp it) so it can be decided "
            "on again. It never approves a plan."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the outcomes without writing, auditing or notifying anything",
    )
    args = parser.parse_args(argv)

    result = run(dry_run=args.dry_run)
    for entry in result["plans"]:
        print(json.dumps(entry))
    print(json.dumps(result["summary"]))
    return 1 if result["summary"]["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
