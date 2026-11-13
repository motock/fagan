"""The one-time import of file-store audit chains into the SQLite audit table.

ADR 0012 decision 10: the hash chain is unchanged by the move.  A chain is
copied row for row -- never re-appended, because a re-append would re-link every
record and change every hash, so the heads a file anchor recorded would no
longer match.

Each chain is decided on its own, and nothing is written for a chain until it
has been verified:

* ``imported`` -- the chain verified and was written; the table's head is the
  file's head.
* ``already_present`` -- the table holds the identical chain; nothing written.
  This is what makes a second run a no-op.
* ``refused`` -- with a reason code and nothing written for that chain:
  ``unverifiable`` (a malformed line or a failed check, with the first bad
  ``seq``), ``conflict`` (the table holds a different chain of that name),
  ``invalid_name`` (the name is not a record key the store can hold),
  ``unreadable`` (the file cannot be read), ``anchor_mismatch`` (an anchor is
  configured and the chain is shorter than its anchored ``seq``, or its hash
  there differs from the anchor), ``anchor_unreadable`` (an anchor sink is
  configured but cannot be read, so no chain may be cleared against it).

A broken chain is refused and never laundered into a fresh table; the other
chains still import.  Record contents never reach a report or a log.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pipeline import audit_chain
from pipeline.audit_anchor import (
    HOST_CHAIN,
    AnchorUnreadable,
    _anchors,
    build_anchor_sink,
)
from pipeline.audit_log import HOST_CHAIN_ID, validate_chain
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError
from pipeline.store_sqlite_audit import AuditChainConflictError

logger = logging.getLogger(__name__)

IMPORTED = "imported"
ALREADY_PRESENT = "already_present"
REFUSED = "refused"


def import_audit_chains(
    source: Any,
    target: Any,
    namespace: str = DEFAULT_NAMESPACE,
    dry_run: bool = False,
) -> list[dict]:
    """Import every chain of *namespace* from *source* into *target*.

    *source* is a :class:`pipeline.audit_file.FileAuditLog` and *target* a
    :class:`pipeline.store_sqlite_audit.SqliteAuditLog`.  ``dry_run`` decides
    and reports every chain exactly as a real run would, and writes nothing.
    The source is only ever read.

    The anchor is read from the configured sink, once per call.  With no sink
    configured no chain is checked against an anchor; a configured sink that
    cannot be read refuses every chain.

    Returns one ``{"chain", "records", "status"}`` entry per chain, plus a
    ``"reason"`` for a refusal and a ``"seq"`` for an unverifiable chain.  Each
    stream the source skips as unaddressable is an ``invalid_name`` refusal.  An
    unknown namespace raises before any stream is read.
    """
    chains = source.list_chains(namespace)
    anchors, anchors_unreadable = _configured_anchors()
    report = [
        _import_one(
            source, target, namespace, chain, dry_run, anchors, anchors_unreadable
        )
        for chain in chains
    ]
    # A stream the source skips is refused, not silently dropped: the report must
    # account for every file the source holds.
    report.extend(
        _refused(name, 0, "invalid_name")
        for name in source.unaddressable_chains(namespace)
    )
    return report


def _configured_anchors() -> tuple[Mapping[str, tuple[int, str]] | None, bool]:
    """The anchored heads of the configured sink, and whether it is unreadable.

    ``(None, False)`` means no sink is configured.  An invalid or unreadable
    sink gives ``(None, True)``: nothing may be cleared against an anchor the
    import cannot read.  The ratchet and chain-name validation are the audit
    anchor's own, so they apply here as they do to verification.
    """
    try:
        sink = build_anchor_sink()
        if sink is None:
            return None, False
        return _anchors(sink), False
    except (AnchorUnreadable, ValueError):
        return None, True


def _import_one(
    source: Any,
    target: Any,
    namespace: str,
    chain: str,
    dry_run: bool,
    anchors: Mapping[str, tuple[int, str]] | None,
    anchors_unreadable: bool,
) -> dict:
    """Decide and, unless ``dry_run``, write one chain."""
    try:
        validate_chain(chain)
    except InvalidRecordKeyError:
        # The name is not logged: a plan name can be operator data.
        logger.warning("refused an audit chain whose name is not a record key")
        return _refused(chain, 0, "invalid_name")

    try:
        entries = source.read_chain(namespace, chain)
    except (OSError, UnicodeDecodeError):
        logger.warning("refused an audit chain that cannot be read")
        return _refused(chain, 0, "unreadable")

    bad = audit_chain.verify_entries(entries)
    if bad is not None:
        logger.warning("refused an audit chain that fails verification")
        return {**_refused(chain, len(entries), "unverifiable"), "seq": bad}

    if anchors_unreadable:
        logger.warning("refused an audit chain: the configured anchor cannot be read")
        return _refused(chain, len(entries), "anchor_unreadable")
    if anchors is not None and not _agrees_with_anchor(chain, entries, anchors):
        logger.warning("refused an audit chain that disagrees with its anchor")
        return _refused(chain, len(entries), "anchor_mismatch")

    existing = target.read_chain(namespace, chain)
    if existing:
        # Identical in every record, which includes the head and the length.
        if existing == entries:
            return _entry(chain, len(entries), ALREADY_PRESENT)
        logger.warning("refused an audit chain that differs from the table")
        return _refused(chain, len(entries), "conflict")

    if dry_run:
        return _entry(chain, len(entries), IMPORTED)
    try:
        target.import_chain(namespace, chain, entries)
    except AuditChainConflictError:
        return _refused(chain, len(entries), "conflict")
    except ValueError:
        return _refused(chain, len(entries), "unverifiable")
    return _entry(chain, len(entries), IMPORTED)


def _agrees_with_anchor(
    chain: str, entries: list[dict], anchors: Mapping[str, tuple[int, str]]
) -> bool:
    """Whether *entries* reach the anchored ``seq`` with the anchored hash.

    A chain with no anchor agrees by definition.  The import names the host
    chain ``@host`` while the anchor log keys it :data:`HOST_CHAIN`.
    """
    key = HOST_CHAIN if chain == HOST_CHAIN_ID else chain
    anchored = anchors.get(key)
    if anchored is None:
        return True
    seq, digest = anchored
    for record in entries:
        if record["seq"] == seq:
            return record["hash"] == digest
    return False


def _entry(chain: str, records: int, status: str) -> dict:
    return {"chain": chain, "records": records, "status": status}


def _refused(chain: str, records: int, reason: str) -> dict:
    return {**_entry(chain, records, REFUSED), "reason": reason}
