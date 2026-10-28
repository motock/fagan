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

Losing sight of the anchor log is itself tamper evidence. A missing log is an
empty one - anchoring has simply not started - but a log that exists and cannot
be read raises :class:`AnchorUnreadable` rather than reading as empty: treating
"cannot be read" as "nothing was anchored" would both clear the verdict on a
truncated chain and let the next pass record the already-cut head as the anchor,
laundering the truncation permanently. So verification reports
``"anchor log unreadable: <OSError type>"`` and an anchoring pass appends nothing,
leaving the previous anchors - and the previous verdict - in force.

The sink file is part of that boundary, so it is opened ``O_NOFOLLOW`` and its
mode is re-imposed to ``0o600`` on the open descriptor: a pre-existing anchor
file with a looser mode is tightened, and a symlink planted in a writable sink
directory is refused rather than followed, so a write cannot be steered onto a
file the operator did not designate.

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

#: Report key for a sink-level failure in :func:`anchor_report`. An unreadable
#: anchor log cannot say which chains it held, and an empty report reads as a
#: clean one, so the verdict is filed under this key instead. It is never a chain
#: name: a chain is a bare file name, and ``*`` cannot be one.
SINK_VERDICT = "*"


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


class AnchorUnreadable(RuntimeError):
    """The anchor log exists but could not be read.

    Carries the ``OSError`` type name only: never the path, the exception text,
    or anything else that could leak the sink's location into a log line.
    """

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _usable_chain(chain: object) -> str | None:
    """*chain* when it names a stream directly inside ``PLAN_DIR``, else ``None``.

    An anchor line is input from outside the pipeline, so its ``chain`` value is
    validated like any other boundary value: only a bare name resolves to
    ``PLAN_DIR/<chain>.audit.jsonl``. A ``../`` or absolute name would steer the
    read somewhere else entirely, so it is not an anchor at all.
    """
    if not isinstance(chain, str) or not chain:
        return None
    if chain in {".", "..", "*"} or "/" in chain or "\\" in chain or "\x00" in chain:
        return None
    if Path(chain).name != chain:
        return None
    return chain


def _read_records(path: Path, *, context: str) -> Iterator[dict[str, Any]]:
    """Stream the well-formed records of *path*, in file order.

    The anchor log grows without bound, so it is streamed line by line instead
    of being read into memory. A missing file yields nothing; an unreadable one
    is reported once - naming *context* (a chain name) and the error type, never
    the path or the exception text - and yields nothing, so one damaged stream
    cannot abort a whole pass.

    This is the fail-soft reader for *chain* streams only. The anchor log itself
    is read through :func:`_anchor_records`, because there "unreadable" must not
    be mistaken for "empty".
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            yield from _parsed(handle)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        logger.warning("could not read the audit stream for %s: %s", context, type(exc).__name__)


def _parsed(handle: Any) -> Iterator[dict[str, Any]]:
    """The well-formed JSON objects among *handle*'s lines, in file order."""
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


def _anchor_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream the anchor log's records, in file order.

    A missing log is an empty log. One that exists but cannot be read - a mode or
    ACL change, a broken export, a stream read that fails mid-pass - raises
    :class:`AnchorUnreadable`, including when the failure lands part-way through
    the file: a half-read anchor set must not be mistaken for the whole one.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            yield from _parsed(handle)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise AnchorUnreadable(type(exc).__name__) from None


def _anchors(path: Path) -> dict[str, tuple[int, str]]:
    """The strongest ``(seq, hash)`` anchor recorded per chain.

    The strongest is the highest ``seq`` ever anchored and, at equal ``seq``, the
    hash anchored first. Anchoring therefore ratchets: a later pass can only
    raise a chain's anchor, never lower it or replace the hash already vouched
    for, so a cut tail or a rewritten record stays visible on every following
    tick instead of being laundered by the next one. A line whose ``chain`` is
    not a bare stream name, or without an integer ``seq`` and a non-empty
    ``hash``, is not an anchor and is ignored.

    Raises:
        AnchorUnreadable: The log could not be read. The anchor set is unknown,
            never empty.
    """
    strongest: dict[str, tuple[int, str]] = {}
    for record in _anchor_records(path):
        chain = _usable_chain(record.get("chain"))
        seq = record.get("seq")
        digest = record.get("hash")
        if (
            chain is None
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
    """Append one record with ``O_APPEND``; the log is never rewritten.

    The sink is opened ``O_NOFOLLOW`` and its mode is re-imposed to ``0o600`` on
    the open descriptor rather than only at creation. A mode argument applies
    only when ``open`` creates the file: a pre-existing anchor log with a looser
    mode would otherwise stay group- or world-readable, and a symlink planted in
    a writable sink directory would redirect the append somewhere else entirely.
    A refusal surfaces as ``OSError`` and the caller appends nothing - the sink
    is part of the tamper-evidence boundary, so it fails closed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
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
    try:
        known = _anchors(path)
    except AnchorUnreadable as exc:
        # The anchors cannot be read, so the current head cannot be compared with
        # them: appending would treat the anchor set as empty and record a head
        # below an already anchored one, laundering a cut tail. Append nothing.
        logger.warning(
            "the anchor log could not be read: %s; no head was anchored", exc.error_type
        )
        return 0
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
        ``"anchor log unreadable: <OSError type>"`` when the sink could not be
        read: the anchors are unknown, so the chain cannot be cleared.
        Anchors never lower, so a verdict does not clear itself on a later pass.
    """
    path = _anchor_path()
    if path is None:
        return None
    try:
        anchored = _anchors(path).get(chain)
    except AnchorUnreadable as exc:
        return f"anchor log unreadable: {exc.error_type}"
    if anchored is None:
        return None
    seq, digest = anchored
    return _verdict(chain, seq, digest)


def anchor_report() -> dict[str, str | None] | None:
    """A verdict per anchored chain, or ``None`` when anchoring is off.

    The chains come from the anchor log rather than the plan directory, so a
    chain whose stream was deleted outright is reported as truncated instead of
    disappearing from the report. When the log itself cannot be read the report
    is a single :data:`SINK_VERDICT` entry rather than an empty mapping, because
    an empty mapping reads as "nothing was anchored" - a clean verdict.
    """
    path = _anchor_path()
    if path is None:
        return None
    try:
        anchored = _anchors(path)
    except AnchorUnreadable as exc:
        return {SINK_VERDICT: f"anchor log unreadable: {exc.error_type}"}
    return {chain: _verdict(chain, seq, digest) for chain, (seq, digest) in anchored.items()}
