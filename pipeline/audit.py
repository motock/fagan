"""Append-only, hash-chained audit stream (enterprise readiness phase 5).

One file per plan, ``<PLAN_DIR>/<plan>.audit.jsonl``: a JSON object per line,
each carrying the hash of the record before it. Rewriting, reordering, or
deleting a record in the middle makes ``verify_audit_chain`` return that
record's index.

What this does NOT detect: truncation of the tail. A chain with no external
anchor cannot prove that the newest records were not removed, so the head hash
also has to be shipped somewhere the pipeline cannot rewrite. Say this wherever
the stream is documented rather than implying the chain is stronger than it is.

Records carry identifiers and outcomes only - never file contents, prompts, or
secrets. ``detail`` is for short structural fields: a digest, a role, a sha.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import persistence
from .actor import actor_id, current_correlation_id

logger = logging.getLogger("pipeline.audit")


def audit_path(plan_name: str) -> Path:
    """Return the plan's audit-stream path.

    ``persistence.PLAN_DIR`` is read at call time, not captured at import, so
    the fixtures that already redirect ``pipeline.persistence.PLAN_DIR`` at a
    temp directory redirect this stream with them instead of letting a test
    append to the operator's real plan directory.
    """
    return persistence.PLAN_DIR / f"{plan_name}.audit.jsonl"


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _record_hash(record: dict[str, Any]) -> str:
    """Hash every field except the record's own ``hash``."""
    payload = {key: value for key, value in record.items() if key != "hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _tail(path: Path) -> dict[str, Any] | None:
    """Return the last well-formed record, or ``None`` for a missing file."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None
    return None


#: The host-scoped stream: events that have no plan to belong to.
HOST_AUDIT_FILE = ".audit.host.jsonl"


def host_audit_path() -> Path:
    """Return the host-scoped audit stream, for events that have no plan."""
    return persistence.PLAN_DIR / HOST_AUDIT_FILE


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
    concurrent appends cannot claim the same ``seq`` or the same
    ``prev_hash``. Reading the file under that lock is deliberate: the chain
    head is then derived from the bytes on disk rather than from a second
    "last hash" file that could drift out of step with them.
    """
    return _append_at(
        audit_path(plan_name),
        action=action,
        outcome=outcome,
        target=target,
        detail=detail,
    )


def append_host_audit(
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one record to the host-scoped chain (no plan exists to scope it)."""
    return _append_at(
        host_audit_path(), action=action, outcome=outcome, target=target, detail=detail
    )


def _append_at(
    path: Path,
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The one append implementation, for a chain at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        previous = _tail(path)
        record: dict[str, Any] = {
            "seq": 0 if previous is None else int(previous.get("seq", 0)) + 1,
            "prev_hash": "" if previous is None else previous.get("hash", ""),
            "ts": datetime.now(UTC).isoformat(),
            "actor": actor_id(),
            "correlation_id": current_correlation_id(),
            "action": action,
            "outcome": outcome,
            "target": dict(target or {}),
            "detail": dict(detail or {}),
        }
        record["hash"] = _record_hash(record)
        os.write(fd, (_canonical(record) + "\n").encode("utf-8"))
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_audit(
    plan_name: str,
    *,
    action: str,
    outcome: str,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one record without ever changing the caller's decision.

    Call sites are authorization and state-transition paths: a full disk or a
    read-only plan directory must degrade the audit trail loudly, not turn an
    allowed merge into a failed one or a denied action into an allowed one. The
    failure is logged at ERROR with the action and plan so the gap is visible.
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


def verify_audit_chain(plan_name: str) -> int | None:
    """Return the seq of the first altered record, or ``None`` when intact."""
    return _verify_chain_at(audit_path(plan_name))


def verify_host_audit_chain() -> int | None:
    """Return the seq of the first altered host record, or ``None`` when intact."""
    return _verify_chain_at(host_audit_path())


def _verify_chain_at(path: Path) -> int | None:
    """Return the seq of the first altered record in *path*, or ``None``.

    Checks all three links a rewrite would break: the running ``seq``, the
    ``prev_hash`` of the previous line, and the record's own ``hash``. A line
    that is not valid JSON is reported as broken at its recorded position. A
    missing or blank file is intact: there is no record to disagree with.
    """
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None
    previous_hash = ""
    index = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return index
        if not isinstance(record, dict):
            return index
        if record.get("seq") != index:
            return index
        if record.get("prev_hash") != previous_hash:
            return index
        if record.get("hash") != _record_hash(record):
            return index
        previous_hash = str(record.get("hash", ""))
        index += 1
    return None
