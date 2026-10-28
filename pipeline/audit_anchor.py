"""External anchor for the audit-chain heads (enterprise readiness AA-02).

``pipeline.audit`` detects a rewritten, reordered, or mid-stream-deleted record,
but it cannot detect truncation of the tail: a chain whose newest records were
removed still verifies intact, because the stream itself has no record of where
it ended. :data:`PIPELINE_AUDIT_ANCHOR` closes that hole by repeatedly shipping
each chain's ``(seq, hash)`` head to an append-only log the pipeline host cannot
rewrite.

The sink only buys anything where the pipeline cannot reach: a WORM mount, an
object-lock bucket exported by another host, or a separate host's NFS export
with append-only permissions. A plain file on the same host as ``PLAN_DIR``
raises the bar - a forger has to edit or delete anchor lines as well as chain
lines - but the same principal who can rewrite the chain can rewrite the file,
so it is not tamper-proof evidence. An HTTP or S3 sink is a later story.

Off by default: unset or empty means no anchor file is created and verification
reports nothing. ``file:<absolute path>`` is the only accepted form; any other
value fails closed with :class:`ValueError` at first use (ADR 0004) rather than
silently degrading to no anchoring. The message names the variable only, never
its value, because a sink URI may carry a credential.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import audit, persistence

logger = logging.getLogger("pipeline.audit_anchor")

#: The environment variable that turns anchoring on and names the sink.
PIPELINE_AUDIT_ANCHOR = "PIPELINE_AUDIT_ANCHOR"

#: The chain name used for the host-scoped stream (events with no plan).
HOST_CHAIN = "host"


def _anchor_path() -> Path | None:
    """Resolve the configured sink, or ``None`` when anchoring is off.

    Raises:
        ValueError: The variable holds anything but ``file:<absolute path>``
            outside ``PLAN_DIR``. The message names the variable, never the
            configured value.
    """
    raw = os.environ.get(PIPELINE_AUDIT_ANCHOR, "").strip()
    if not raw:
        return None
    scheme, separator, target = raw.partition(":")
    if scheme != "file" or not separator or not target:
        raise ValueError(
            f"{PIPELINE_AUDIT_ANCHOR} must be 'file:<absolute path>'; "
            "audit-head anchoring is off until it is corrected"
        )
    path = Path(target).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"{PIPELINE_AUDIT_ANCHOR} 'file:' path must be absolute; "
            "audit-head anchoring is off until it is corrected"
        )
    if path.resolve().is_relative_to(persistence.PLAN_DIR.expanduser().resolve()):
        raise ValueError(
            f"{PIPELINE_AUDIT_ANCHOR} 'file:' path must be outside the plan directory; "
            "an anchor the pipeline can rewrite proves nothing"
        )
    return path


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _records(path: Path) -> list[dict[str, Any]]:
    """Every well-formed record in *path*, in file order; nothing for a missing file."""
    try:
        text = path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _latest_anchors(path: Path) -> dict[str, dict[str, Any]]:
    """The most recent anchor record per chain name."""
    latest: dict[str, dict[str, Any]] = {}
    for record in _records(path):
        chain = record.get("chain")
        if isinstance(chain, str):
            latest[chain] = record
    return latest


def _chain_streams() -> Iterator[tuple[str, Path]]:
    """Yield ``(chain name, audit-stream path)`` for every plan and the host."""
    probe = audit.audit_path("")  # <PLAN_DIR>/<plan>.audit.jsonl
    suffix = probe.name
    for path in sorted(probe.parent.glob(f"*{suffix}")):
        yield path.name.removesuffix(suffix), path
    yield HOST_CHAIN, audit.host_audit_path()


def _stream_for(chain: str) -> Path:
    return audit.host_audit_path() if chain == HOST_CHAIN else audit.audit_path(chain)


def _head(path: Path) -> tuple[int, str] | None:
    """The ``(seq, hash)`` of the last usable record, or ``None``."""
    records = _records(path)
    if not records:
        return None
    seq = records[-1].get("seq")
    digest = records[-1].get("hash")
    if not isinstance(seq, int) or not isinstance(digest, str) or not digest:
        return None
    return seq, digest


def _append(path: Path, record: dict[str, Any]) -> None:
    """Append one record with ``O_APPEND``; the log is never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (_canonical(record) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def anchor_heads() -> int:
    """Anchor every chain head that changed since its last anchor record.

    Appends ``{"chain": <plan|"host">, "seq": <head seq>, "hash": <head hash>,
    "at": <utc iso>}`` for each plan chain and the host chain whose head moved.
    An unchanged head adds nothing, so a daemon that calls this every tick does
    not grow the log while the stream is quiet.

    Returns:
        How many anchor records were appended; ``0`` when anchoring is off.
    """
    path = _anchor_path()
    if path is None:
        return 0
    known = _latest_anchors(path)
    appended = 0
    for chain, stream in _chain_streams():
        head = _head(stream)
        if head is None:
            continue
        seq, digest = head
        prior = known.get(chain)
        if prior is not None and prior.get("seq") == seq and prior.get("hash") == digest:
            continue
        _append(path, {"chain": chain, "seq": seq, "hash": digest, "at": datetime.now(UTC).isoformat()})
        appended += 1
    return appended


def _verdict(chain: str, anchored: dict[str, Any]) -> str | None:
    """Compare *chain* with one anchored ``(seq, hash)`` record."""
    seq = anchored.get("seq")
    digest = anchored.get("hash")
    for record in _records(_stream_for(chain)):
        if record.get("seq") == seq:
            if record.get("hash") == digest:
                return None
            return f"anchored hash mismatch at seq {seq}"
    return f"truncated below anchored seq {seq}"


def verify_against_anchor(chain: str) -> str | None:
    """Compare *chain* with the last head anchored for it.

    Returns:
        ``None`` when anchoring is off, when nothing has been anchored for
        *chain* yet, or when the anchored ``(seq, hash)`` is still in the chain.
        Otherwise the tamper verdict: ``"truncated below anchored seq N"`` when
        the chain no longer reaches the anchored record - the tail was cut - or
        ``"anchored hash mismatch at seq N"`` when that record was edited.
    """
    path = _anchor_path()
    if path is None:
        return None
    anchored = _latest_anchors(path).get(chain)
    if anchored is None:
        return None
    return _verdict(chain, anchored)


def _anchor_report() -> dict[str, str | None] | None:
    """A verdict per anchored chain, or ``None`` when anchoring is off.

    The chains come from the anchor log rather than the plan directory, so a
    chain whose stream was deleted outright is reported as truncated instead of
    disappearing from the report.
    """
    path = _anchor_path()
    if path is None:
        return None
    return {
        chain: _verdict(chain, anchored)
        for chain, anchored in _latest_anchors(path).items()
    }
