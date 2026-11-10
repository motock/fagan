"""External anchor for the audit-chain heads (enterprise readiness AA-02).

``pipeline.audit`` detects a rewritten, reordered, or mid-stream-deleted record,
but it cannot detect truncation of the tail: a chain whose newest records were
removed still verifies intact, because the stream itself has no record of where
it ended. :data:`PIPELINE_AUDIT_ANCHOR` closes that hole by repeatedly shipping
each chain's ``(seq, hash)`` head to an append-only log the pipeline host cannot
rewrite.

The sink only buys anything where the pipeline cannot reach: a WORM mount, an
object-lock bucket exported by another host, or a separate host's NFS export
with append-only permissions. A plain file on the same host as the plan
directory raises the bar - a forger has to edit or delete anchor lines as well
as chain lines - but the same principal who can rewrite the chain can rewrite
the file, so it is not tamper-proof evidence.

The sink is an :class:`AnchorSink`: an object offering ``publish`` and
``read_heads``, so an object-lock bucket, a syslog relay or a SIEM can be
plugged in without this module knowing. ``file:<absolute path>`` is the
built-in one (:class:`FileAnchorSink`); an HTTP or S3 sink is a later story.
A sink is deliberately dumb: it stores a head and returns stored heads raw. The
ratchet and the chain-name validation stay in this module (``_anchors`` and
``_usable_chain``), so a plugin cannot weaken them by publishing a cleaned-up
view of what it holds.

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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from . import audit, decision_records
from .audit_log import HOST_CHAIN_ID
from .store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

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


@dataclass(frozen=True)
class AnchorHead:
    """One anchored head, exactly as a sink stored it.

    Raw on purpose: a sink reports what it holds and nothing more. The ratchet
    and the chain-name validation live in this module, so a plugin cannot
    launder a tampered head by returning a "cleaned" view of its log.
    """

    namespace: str
    chain: str
    seq: int
    hash: str


class AnchorSink(Protocol):
    """Where anchor heads are shipped: append-only from the pipeline's side.

    There is no method that rewrites, reorders or deletes an anchor, and no
    attribute that exposes the storage for mutation. A sink may be an
    object-lock bucket, a syslog relay or a SIEM; the built-in one is
    :class:`FileAnchorSink`.
    """

    def publish(self, namespace: str, chain: str, seq: int, head_hash: str) -> None:
        """Append one ``(chain, seq, head_hash)`` anchor, never rewriting one."""
        ...

    def read_heads(self) -> Iterator[AnchorHead]:
        """Yield every stored head, in publication order."""
        ...


class FileAnchorSink:
    """The built-in sink: one append-only JSONL file.

    The file is part of the tamper-evidence boundary, so it is opened
    ``O_NOFOLLOW`` and its mode is re-imposed to ``0o600`` on the open
    descriptor (see :func:`_append`), and a log that exists but cannot be read
    raises :class:`AnchorUnreadable` rather than reading as empty.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def publish(self, namespace: str, chain: str, seq: int, head_hash: str) -> None:
        """Append one anchor line for ``(chain, seq, head_hash)``.

        The timestamp is stamped here exactly as :func:`anchor_heads` always
        stamped it, so the line stays byte-identical to the one an older
        pipeline wrote for the same head.

        Raises:
            UnknownNamespaceError: This sink stores the default namespace only.
                Nothing is written.
            OSError: The sink refused the append - a symlinked or unwritable
                path. The anchor is not recorded.
        """
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError("unknown namespace")
        _append(
            self.path,
            {
                "chain": chain,
                "seq": seq,
                "hash": head_hash,
                "at": datetime.now(UTC).isoformat(),
            },
        )

    def read_heads(self) -> Iterator[AnchorHead]:
        """Yield the stored heads in publication order.

        Raises:
            AnchorUnreadable: The log could not be read, including when the
                failure lands part-way through it.
        """
        for record in _anchor_records(self.path):
            chain = record.get("chain")
            seq = record.get("seq")
            digest = record.get("hash")
            if not isinstance(chain, str) or not chain:
                continue
            if not isinstance(seq, int) or isinstance(seq, bool):
                continue
            if not isinstance(digest, str) or not digest:
                continue
            yield AnchorHead(DEFAULT_NAMESPACE, chain, seq, digest)


#: ``configured`` -- a sink is configured and usable.
ANCHOR_CONFIGURED = "configured"
#: ``unconfigured`` -- no sink, and this deployment is not the file store.
ANCHOR_UNCONFIGURED = "unconfigured"
#: ``off`` -- no sink, and this deployment is the file store (its default).
ANCHOR_OFF = "off"
#: ``invalid`` -- a sink is configured but the value cannot be used.
ANCHOR_INVALID = "invalid"

#: The four fixed words :func:`anchor_status` can return.
ANCHOR_STATUSES: tuple[str, ...] = (
    ANCHOR_CONFIGURED,
    ANCHOR_UNCONFIGURED,
    ANCHOR_OFF,
    ANCHOR_INVALID,
)

#: Set once the "no external anchor" notice has been logged in this process.
_UNCONFIGURED_NOTICE_LOGGED = False


def anchor_status(environ: Mapping[str, str] | None = None) -> str:
    """Return which of the four anchor states this deployment is in.

    The answer is one fixed word (:data:`ANCHOR_STATUSES`) and nothing else: a
    health payload is readable by anything that can reach the API, so neither
    the configured value, the sink path nor the text of a failure is returned
    here -- only which of the four states applies. Nothing is cached, so an
    operator who fixes the configuration does not have to restart to see it.

    ``configured`` needs a sink this module can build; a value it refuses is
    ``invalid``. With no sink, the file store is ``off`` (its audit trail was
    never going to leave the host) and any other store -- including a selection
    that cannot be resolved at all -- is ``unconfigured``, since that store
    could have been anchored and is not. The unconfigured case logs one warning
    per process, naming only the store kind: a health probe runs on every
    request and must not flood the log.
    """
    try:
        sink = build_anchor_sink(environ)
    except ValueError:
        return ANCHOR_INVALID
    if sink is not None:
        return ANCHOR_CONFIGURED
    from pipeline.store import reported_store_kind

    kind = reported_store_kind(os.environ if environ is None else environ)
    if kind == "file":
        return ANCHOR_OFF
    global _UNCONFIGURED_NOTICE_LOGGED
    if not _UNCONFIGURED_NOTICE_LOGGED:
        _UNCONFIGURED_NOTICE_LOGGED = True
        logger.warning(
            "audit heads are not externally anchored on this deployment "
            "(store: %s); configure %s to anchor them",
            kind,
            PIPELINE_AUDIT_ANCHOR,
        )
    return ANCHOR_UNCONFIGURED


def build_anchor_sink(environ: Mapping[str, str] | None = None) -> AnchorSink | None:
    """Build the configured sink, or ``None`` when anchoring is off.

    Args:
        environ: The environment to read; defaults to :data:`os.environ`. Read
            on every call, never captured at import, so a fixture that
            redirects it redirects the next pass.

    Raises:
        ValueError: The variable holds anything but ``file:<absolute path>``
            outside the plan directory. The message names the variable, never
            the configured value, because a sink URI may carry a credential.
    """
    env = os.environ if environ is None else environ
    raw = env.get(PIPELINE_AUDIT_ANCHOR, "").strip()
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
    # The plan directory is resolved through the audit facade at call time, so
    # a redirected plan directory redirects this refusal with it.
    plan_directory = audit.audit_path("").parent
    if path.resolve().is_relative_to(plan_directory.expanduser().resolve()):
        raise ValueError(
            f"{PIPELINE_AUDIT_ANCHOR} 'file:' path must be outside the plan directory; "
            "an anchor the pipeline can rewrite proves nothing"
        )
    return FileAnchorSink(path)


class AnchorUnreadable(RuntimeError):
    """The anchor log exists but could not be read.

    Carries the ``OSError`` type name only: never the path, the exception text,
    or anything else that could leak the sink's location into a log line.
    """

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _usable_chain(chain: object) -> str | None:
    """*chain* when it names a stream directly inside the plan directory.

    An anchor line is input from outside the pipeline, so its ``chain`` value is
    validated like any other boundary value: only a bare name resolves to
    ``<chain>.audit.jsonl`` in the plan directory. A ``../`` or absolute name
    would steer the read somewhere else entirely, so it is not an anchor at all.
    """
    if not isinstance(chain, str) or not chain:
        return None
    if chain in {".", "..", "*"} or "/" in chain or "\\" in chain or "\x00" in chain:
        return None
    if Path(chain).name != chain:
        return None
    # ``decisions:<plan>`` is the anchor key for a decisions chain; a bare chain
    # name is a plan's audit stream. ':' is not a plan-name character
    # (``pipeline.parsers._validate_key`` allows only [A-Za-z0-9._-]), so a colon
    # in the plan part means the key aliases the other namespace: plan
    # ``decisions:other`` and the decisions chain of plan ``other`` would both be
    # ``decisions:other``, collapsing to one entry here and making _verdict
    # compare that anchor against the wrong file - a permanent false "truncated"
    # verdict, or a real cut masked. Such a key is not an anchor.
    plan = chain.removeprefix("decisions:") if chain.startswith("decisions:") else chain
    if not plan or ":" in plan:
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


def _anchors(sink: AnchorSink) -> dict[str, tuple[int, str]]:
    """The strongest ``(seq, hash)`` anchor recorded per chain.

    The strongest is the highest ``seq`` ever anchored and, at equal ``seq``, the
    hash anchored first. Anchoring therefore ratchets: a later pass can only
    raise a chain's anchor, never lower it or replace the hash already vouched
    for, so a cut tail or a rewritten record stays visible on every following
    tick instead of being laundered by the next one. A line whose ``chain`` is
    not a bare stream name, or without an integer ``seq`` and a non-empty
    ``hash``, is not an anchor and is ignored.

    The chain-name validation and the ratchet stay here rather than in the
    sink, so a plugin cannot weaken either: a head whose ``chain`` is not a bare
    stream name is not an anchor, and a lower ``seq`` never lowers the anchor.

    Raises:
        AnchorUnreadable: The log could not be read. The anchor set is unknown,
            never empty.
    """
    strongest: dict[str, tuple[int, str]] = {}
    for head in sink.read_heads():
        chain = _usable_chain(head.chain)
        if chain is None:
            continue
        prior = strongest.get(chain)
        if prior is None or head.seq > prior[0]:
            strongest[chain] = (head.seq, head.hash)
    return strongest


def _stream_for(chain: str) -> Path:
    """The stream a chain's anchor key names.

    The anchor key is the plan name for a plan chain, ``"host"`` for the host
    chain and ``decisions:<plan>`` for a decisions chain, so a plan actually
    named ``decisions`` is still read as its own audit stream rather than as the
    decisions namespace. The path is resolved by the store, not built here.
    """
    if chain.startswith("decisions:"):
        return decision_records.decision_stream_path(chain.removeprefix("decisions:"))
    log = audit._file_log()
    return log.stream_path(HOST_CHAIN_ID if chain == HOST_CHAIN else chain)


def _chain_streams() -> Iterator[tuple[str, Path]]:
    """Yield ``(anchor key, stream path)`` for every chain the log holds.

    The plan and host chains are enumerated through the audit log rather than by
    globbing the plan directory, so a chain kept anywhere else is anchored the
    same way; the decisions chains come from their own helper. The anchor key is
    the plan name for a plan chain, ``"host"`` for the host chain and
    ``decisions:<plan>`` for a decisions chain - byte-compatible with the anchor
    log this module has always written.
    """
    log = audit._file_log()
    try:
        chains = log.list_chains(DEFAULT_NAMESPACE)
    except OSError as exc:  # pragma: no cover - a store that cannot be listed
        logger.warning("could not list the audit chains: %s", type(exc).__name__)
        chains = []
    for chain in chains:
        if chain == HOST_CHAIN_ID:
            continue
        # A plan name may not contain ':' (pipeline.parsers._validate_key), and
        # its anchor key would otherwise be indistinguishable from a decisions
        # chain key: plan ``decisions:other`` would anchor as ``decisions:other``,
        # the same key as the decisions chain of plan ``other``. One of the two
        # would never be verified and the surviving anchor would be compared
        # against the wrong file. Skip the unaddressable name rather than alias.
        if ":" in chain:
            logger.warning(
                "skipping an audit stream whose plan name is not addressable plan=%r", chain
            )
            continue
        yield chain, log.stream_path(chain)
    for plan in decision_records.list_decision_chains():
        if ":" in plan:
            logger.warning(
                "skipping a decisions stream whose plan name is not addressable plan=%r", plan
            )
            continue
        yield f"decisions:{plan}", decision_records.decision_stream_path(plan)
    yield HOST_CHAIN, log.stream_path(HOST_CHAIN_ID)


def _chain_heads() -> Iterator[tuple[str, tuple[int, str]]]:
    """Yield ``(anchor key, (seq, hash))`` for every chain with a usable head."""
    log = audit._file_log()
    for chain, path in _chain_streams():
        if chain == HOST_CHAIN:
            head = _head(lambda: log.head(DEFAULT_NAMESPACE, HOST_CHAIN_ID), chain)
        elif chain.startswith("decisions:"):
            head = _head(lambda path=path: audit._tail(path), chain)
        else:
            head = _head(lambda chain=chain: log.head(DEFAULT_NAMESPACE, chain), chain)
        if head is not None:
            yield chain, head


def _head(read: Callable[[], dict[str, Any] | None], chain: str) -> tuple[int, str] | None:
    """The ``(seq, hash)`` the chain's newest record carries, or ``None``.

    The head is read through the audit log, so the log decides where the chain
    lives. An unreadable stream is reported by chain name and error type only,
    and treated as having no head, so one damaged stream cannot abort a pass.
    """
    try:
        record = read()
    except (OSError, InvalidRecordKeyError) as exc:
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
    "at": <utc iso>}`` for each plan chain, the host chain and each decisions
    chain whose head moved forward. An unchanged head adds nothing, so a quiet
    stream does not grow the log. A head that went backwards - the tail was cut
    - or that changed hash at an already anchored ``seq`` - that record was
    rewritten - is *not* anchored: the strongest anchor stands, a WARNING names
    the chain and the two seq values, and the tamper stays reported until an
    operator resolves it.

    A sink that refuses an append raises ``OSError`` out of this function after
    the anchors already published on this pass stand; the caller logs the error
    type, and the next tick retries the chains that did not make it.

    Returns:
        How many anchor records were appended; ``0`` when anchoring is off.
    """
    sink = build_anchor_sink()
    if sink is None:
        return 0
    try:
        known = _anchors(sink)
    except AnchorUnreadable as exc:
        # The anchors cannot be read, so the current head cannot be compared with
        # them: appending would treat the anchor set as empty and record a head
        # below an already anchored one, laundering a cut tail. Append nothing.
        logger.warning(
            "the anchor log could not be read: %s; no head was anchored", exc.error_type
        )
        return 0
    appended = 0
    for chain, (seq, digest) in _chain_heads():
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
        sink.publish(DEFAULT_NAMESPACE, chain, seq, digest)
        appended += 1
    return appended

def _chain_records(chain: str) -> Iterator[dict[str, Any] | None]:
    """The records of *chain* in storage order, or nothing when unreadable.

    The chain is read through the audit log - or, for a decisions chain,
    through the decision-record reader - so the store decides where it lives.
    A stream the pipeline cannot read is reported by chain name and error type
    only and read as empty, so verification calls it truncated instead of
    raising out of a report: one damaged stream must not hide the rest.
    """
    if chain.startswith("decisions:"):
        plan = chain.removeprefix("decisions:")
        try:
            path = decision_records.decision_stream_path(plan)
        except InvalidRecordKeyError as exc:
            logger.warning(
                "could not read the audit stream for %s: %s", chain, type(exc).__name__
            )
            return
        yield from _read_records(path, context=chain)
        return
    stored = HOST_CHAIN_ID if chain == HOST_CHAIN else chain
    try:
        yield from audit._file_log().read_chain(DEFAULT_NAMESPACE, stored)
    except (OSError, InvalidRecordKeyError) as exc:
        logger.warning("could not read the audit stream for %s: %s", chain, type(exc).__name__)


def _verdict(chain: str, seq: int, digest: str) -> str | None:
    """Compare *chain* with one anchored ``(seq, hash)``."""
    for record in _chain_records(chain):
        if record is None:
            continue
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
    sink = build_anchor_sink()
    if sink is None:
        return None
    try:
        anchored = _anchors(sink).get(chain)
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
    sink = build_anchor_sink()
    if sink is None:
        return None
    try:
        anchored = _anchors(sink)
    except AnchorUnreadable as exc:
        return {SINK_VERDICT: f"anchor log unreadable: {exc.error_type}"}
    return {chain: _verdict(chain, seq, digest) for chain, (seq, digest) in anchored.items()}
