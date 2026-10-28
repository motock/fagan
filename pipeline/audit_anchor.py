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

Anchoring only ever ratchets upwards. A chain's anchor is the highest ``seq``
ever recorded for it, and at equal ``seq`` the hash recorded first: a later pass
can raise the anchor but never lower it, and never replace the hash it already
vouched for. A head that went backwards (the tail was cut) or that changed hash
at the anchored ``seq`` (the record was rewritten) is therefore left un-anchored
and keeps failing verification on every following tick, instead of being
laundered by the next one. That also means such a verdict is permanent until an
operator intervenes: a retired chain stays anchored forever, because an
append-only log cannot un-anchor it (a WORM or object-lock sink cannot be edited
at all), so retiring a chain is an operator action on the sink - record the
retirement outside the anchor log, or roll to a fresh sink - never a pipeline
action.

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


def _read_records(path: Path, *, context: str) -> Iterator[dict[str, Any]]:
    """Stream the well-formed records of *path*, in file order.

    The anchor log grows without bound, so it is streamed line by line instead
    of being read into memory. A missing file yields nothing; an unreadable one
    is reported once - naming *context* (a chain name) and the error type, never
    the path or the exception text - and yields nothing, so one damaged stream
    cannot abort a whole pass.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        logger.warning("could not read the audit stream for %s: %s", context, type(exc).__name__)


def _anchors(path: Path) -> dict[str, tuple[int, str]]:
    """The strongest ``(seq, hash)`` anchor recorded per chain.

    The strongest is the highest ``seq`` ever anchored and, at equal ``seq``, the
    hash anchored first. Anchoring therefore ratchets: a later pass can only
    raise a chain's anchor, never lower it or replace the hash already vouched
    for, so a cut tail or a rewritten record stays visible on every following
    tick instead of being laundered by the next one. A line without an integer
    ``seq`` and a non-empty ``hash`` is not an anchor and is ignored.
    """
    strongest: dict[str, tuple[int, str]] = {}
    for record in _read_records(path, context="the anchor log"):
        chain = record.get("chain")
        seq = record.get("seq")
        digest = record.get("hash")
        if (
            not isinstance(chain, str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not isinstance(digest, str)
            or not digest
        ):
            continue
        prior = strongest.get(chain)
        if prior is None or seq > prior[0]:
            strongest[chain] = (seq, digest)
    return strongest


def _chain_streams() -> Iterator[tuple[str, Path]]:
    """Yield ``(chain name, audit-stream path)`` for every plan and the host."""
    probe = audit.audit_path("")  # <PLAN_DIR>/<plan>.audit.jsonl
    suffix = probe.name
    for path in sorted(probe.parent.glob(f"*{suffix}")):
        yield path.name.removesuffix(suffix), path
    yield HOST_CHAIN, audit.host_audit_path()


def _stream_for(chain: str) -> Path:
    return audit.host_audit_path() if chain == HOST_CHAIN else audit.audit_path(chain)


def _head(path: Path, chain: str) -> tuple[int, str] | None:
    """The ``(seq, hash)`` of the chain's last usable record, or ``None``.

    Only the tail is read, so anchoring costs one seek per chain however long
    the stream has grown. An unreadable stream is reported by chain name and
    error type only, and treated as having no head.
    """
    try:
        record = audit._tail(path)
    except OSError as exc:
        logger.warning("could not read the audit stream for %s: %s", chain, type(exc).__name__)
        return None
    if record is None:
        return None
    seq = record.get("seq")
    digest = record.get("hash")
    if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(digest, str) or not digest:
        return None
    return seq, digest


def _append(path: Path, record: dict[str, Any]) -> None:
    """Append one record with ``O_APPEND``; the log is never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (audit._canonical(record) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def anchor_heads() -> int:
    """Anchor every chain head, never lowering an anchor already recorded.

    Appends ``{"chain": <plan|"host">, "seq": <head seq>, "hash": <head hash>,
    "at": <utc iso>}`` for each plan chain and the host chain whose head moved
    forward. An unchanged head adds nothing, so a quiet stream does not grow the
    log. A head that went backwards - the tail was cut - or that changed hash at
    an already anchored ``seq`` - that record was rewritten - is *not* anchored:
    the strongest anchor stands, a WARNING names the chain and the two seq
    values, and the tamper stays reported until an operator resolves it.

    Returns:
        How many anchor records were appended; ``0`` when anchoring is off.
    """
    path = _anchor_path()
    if path is None:
        return 0
    known = _anchors(path)
    appended = 0
    for chain, stream in _chain_streams():
        head = _head(stream, chain)
        if head is None:
            continue
        seq, digest = head
        prior = known.get(chain)
        if prior is not None:
            prior_seq, prior_digest = prior
            if seq < prior_seq:
                logger.warning(
                    "chain %s reports seq %s below the anchored seq %s; keeping the anchored head",
                    chain,
                    seq,
                    prior_seq,
                )
                continue
            if seq == prior_seq:
                if digest != prior_digest:
                    logger.warning(
                        "chain %s changed hash at the anchored seq %s; keeping the anchored hash",
                        chain,
                        seq,
                    )
                continue
        _append(path, {"chain": chain, "seq": seq, "hash": digest, "at": datetime.now(UTC).isoformat()})
        appended += 1
    return appended


def _verdict(chain: str, seq: int, digest: str) -> str | None:
    """Compare *chain* with one anchored ``(seq, hash)``."""
    for record in _read_records(_stream_for(chain), context=chain):
        if record.get("seq") == seq:
            if record.get("hash") == digest:
                return None
            return f"anchored hash mismatch at seq {seq}"
    return f"truncated below anchored seq {seq}"


def verify_against_anchor(chain: str) -> str | None:
    """Compare *chain* with the strongest head anchored for it.

    Returns:
        ``None`` when anchoring is off, when nothing has been anchored for
        *chain* yet, or when the anchored ``(seq, hash)`` is still in the chain.
        Otherwise the tamper verdict: ``"truncated below anchored seq N"`` when
        the chain no longer reaches the anchored record - the tail was cut - or
        ``"anchored hash mismatch at seq N"`` when that record was edited.
        Anchors never lower, so a verdict does not clear itself on a later pass.
    """
    path = _anchor_path()
    if path is None:
        return None
    anchored = _anchors(path).get(chain)
    if anchored is None:
        return None
    seq, digest = anchored
    return _verdict(chain, seq, digest)


def anchor_report() -> dict[str, str | None] | None:
    """A verdict per anchored chain, or ``None`` when anchoring is off.

    The chains come from the anchor log rather than the plan directory, so a
    chain whose stream was deleted outright is reported as truncated instead of
    disappearing from the report.
    """
    path = _anchor_path()
    if path is None:
        return None
    return {
        chain: _verdict(chain, seq, digest)
        for chain, (seq, digest) in _anchors(path).items()
    }
