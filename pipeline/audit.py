"""Append-only, hash-chained audit stream (enterprise readiness phase 5).

One file per plan, ``<plan>.audit.jsonl``, plus one host-scoped stream: a JSON
object per line, each carrying the hash of the record before it. Rewriting,
reordering, or deleting a record in the middle makes ``verify_audit_chain``
return that record's index.

This module is the facade the rest of the pipeline calls. It builds the record
body and delegates everything else - the file layout, the lock, the tail read,
the write - to :class:`pipeline.audit_file.FileAuditLog`, so the process has
exactly one append implementation and one verify implementation. The file log
is selected regardless of ``PIPELINE_STORE``: the sqlite store still delegates
every non-manifest record to the file store (ADR 0012 Context), so moving this
stream into a table without importing the existing JSONL chains would make
verification silently lose history.

What this does NOT detect: truncation of the tail. A chain with no external
anchor cannot prove that the newest records were not removed, so the head hash
also has to be shipped somewhere the pipeline cannot rewrite. Say this wherever
the stream is documented rather than implying the chain is stronger than it is.

Records carry identifiers and outcomes only - never file contents, prompts, or
secrets. ``detail`` is for short structural fields: a digest, a role, a sha.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import audit_chain, persistence
from .actor import actor_id, client_id, current_correlation_id, principal_type
from .audit_log import HOST_CHAIN_ID
from .store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError

logger = logging.getLogger("pipeline.audit")


def audit_path(plan_name: str) -> Path:
    """Return the plan's audit-stream path.

    The plan directory is read at call time, not captured at import, so the
    fixtures that redirect it at a temp directory redirect this stream with
    them instead of letting a test append to the operator's real plan directory.
    """
    return persistence.PLAN_DIR / f"{plan_name}.audit.jsonl"


def _canonical(record: dict[str, Any]) -> str:
    """The canonical JSON the hash is computed over (see ``audit_chain``)."""
    return audit_chain.canonical(record)


def _record_hash(record: dict[str, Any]) -> str:
    """Hash every field except the record's own ``hash`` (see ``audit_chain``)."""
    return audit_chain.record_hash(record)


def _tail(path: Path) -> dict[str, Any] | None:
    """Return the last well-formed record at *path*, or ``None`` (delegate)."""
    from .audit_file import tail_at

    return tail_at(path)


#: The host-scoped stream: events that have no plan to belong to.
HOST_AUDIT_FILE = ".audit.host.jsonl"


def host_audit_path() -> Path:
    """Return the host-scoped audit stream, for events that have no plan."""
    return persistence.PLAN_DIR / HOST_AUDIT_FILE


def _file_log() -> Any:
    """Return the one append/read implementation: the file-backed log.

    Imported inside the call rather than at module scope because
    :mod:`pipeline.audit_file` resolves its streams through this module's
    ``audit_path`` and ``host_audit_path``; a module-level import on both sides
    would break whichever module was imported first.
    """
    from .audit_file import FileAuditLog

    return FileAuditLog()


def _body(
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None,
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the record body: the fields, in the order the writer stamps them.

    ``datetime`` is this module's name, read at call time, so a fixture that
    freezes the clock redirects the timestamp of every record it appends.
    """
    return {
        "ts": datetime.now(UTC).isoformat(),
        "actor": actor_id(),
        "principal_type": principal_type(),
        "client_id": client_id(),
        "correlation_id": current_correlation_id(),
        "action": action,
        "outcome": outcome,
        "target": dict(target or {}),
        "detail": dict(detail or {}),
    }


def append_audit(
    plan_name: str,
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one record to ``plan_name``'s chain and return it.

    An exclusive flock on the stream itself serializes writers, so two
    concurrent appends cannot claim the same ``seq`` or the same ``prev_hash``.
    A chain name that is not an addressable plan name is refused before any
    path is built: the name becomes part of a file name, and ``../x`` would
    otherwise put the chain outside the plan directory.
    """
    return _file_log().append(
        DEFAULT_NAMESPACE,
        plan_name,
        _body(action=action, outcome=outcome, target=target, detail=detail),
    )


def append_host_audit(
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one record to the host-scoped chain (no plan exists to scope it)."""
    return _file_log().append(
        DEFAULT_NAMESPACE,
        HOST_CHAIN_ID,
        _body(action=action, outcome=outcome, target=target, detail=detail),
    )


def record_audit(
    plan_name: str,
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one record without ever changing the caller's decision.

    Call sites are authorization and state-transition paths: a full disk, a
    read-only plan directory or a chain name the layout cannot address must
    degrade the audit trail loudly, not turn an allowed merge into a failed one
    or a denied action into an allowed one. The failure is logged at ERROR with
    the action and plan so the gap is visible; a refused chain name is logged
    without the name, which may be a path.
    """
    try:
        append_audit(
            plan_name, action=action, outcome=outcome, target=target, detail=detail
        )
    except OSError:
        logger.exception(
            "audit append failed action=%s plan=%s - the action itself stands",
            action,
            plan_name,
        )
    except InvalidRecordKeyError:
        logger.error("audit append refused: invalid chain name action=%s", action)


def verify_audit_chain(plan_name: str) -> int | None:
    """Return the seq of the first altered record, or ``None`` when intact."""
    return audit_chain.verify_entries(
        _file_log().read_chain(DEFAULT_NAMESPACE, plan_name)
    )


def verify_audit_log() -> tuple[dict[str, int | None], int | None]:
    """Verify every chain through the log; return ``(plans, host)``.

    Read-only: nothing is appended, rewritten or repaired. *plans* maps each
    plan chain the log lists to the ``seq`` of its first altered record (or
    ``None`` when it is intact), and *host* is the same verdict for the
    host-scoped chain, which belongs to no plan.
    """
    log = _file_log()
    plans: dict[str, int | None] = {}
    for chain in log.list_chains(DEFAULT_NAMESPACE):
        if chain == HOST_CHAIN_ID:
            continue
        plans[chain] = audit_chain.verify_entries(
            log.read_chain(DEFAULT_NAMESPACE, chain)
        )
    host = audit_chain.verify_entries(
        log.read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)
    )
    return plans, host


def verify_host_audit_chain() -> int | None:
    """Return the seq of the first altered host record, or ``None`` when intact."""
    return audit_chain.verify_entries(
        _file_log().read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)
    )


def _verify_chain_at(path: Path) -> int | None:
    """Return the seq of the first altered record in *path*, or ``None``.

    Checks all three links a rewrite would break: the running ``seq``, the
    ``prev_hash`` of the previous line, and the record's own ``hash``. A line
    that is not valid JSON is reported as broken at its recorded position. A
    missing or blank file is intact: there is no record to disagree with.
    """
    from .audit_file import read_chain_at

    return audit_chain.verify_entries(read_chain_at(path))
