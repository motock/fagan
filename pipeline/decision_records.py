"""Decision record v2: one hash-chained stream per plan, anchored (ADR 0014).

An autonomous decision - a high-risk merge, an override of a false-positive
verdict, a widened scope, an edit to an existing test, a triage ruling - is
recorded in the plan directory as ``<plan>.decisions.chain.jsonl``: a JSON object per line,
each carrying the hash of the record before it. The chain is the same shape as
``pipeline.audit``'s (``seq``/``prev_hash``/``hash``) and reuses its canonical
form, its record hash, and its tail reader, so one verifier understands both.

The stream is anchored: ``pipeline.audit_anchor`` ships each chain's
``(seq, hash)`` head to storage this host cannot rewrite, which is what makes a
*truncated* tail detectable. The chain alone cannot see that, so say "anchored"
whenever this stream is described as tamper-evident rather than implying the
hash links are enough.

What belongs in a record is deliberately narrow: identifiers, digests, the rule
that fired, the alternatives considered, and the ruling. Never a prompt, a
payload, a secret, a token, or a body - a decision record is audit evidence, not
a transcript. The limits are enforced rather than documented: every string is
capped, prompt-shaped keys are refused at any depth, and a rejection names the
offending *field*, never its value, so a validator error cannot leak the content
it refused into a log line.

Who made the decision is established by the transport, never by the caller:
``actor``, ``principal_type`` and ``client_id`` come from
:mod:`pipeline.actor`, exactly as they do for the audit stream. A record whose
caller supplies its own identity is refused *before* anything is written, so a
refused append neither consumes a ``seq`` nor leaves an unauthorized principal
in the chain.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import audit
from .actor import (
    ANONYMOUS,
    actor_id,
    client_id,
    current_correlation_id,
    current_principal,
    principal_type,
)

logger = logging.getLogger("pipeline.decision_records")

#: The record schema this module writes. Bumped when a field changes meaning.
SCHEMA_VERSION = 2

#: The only namespace the core pipeline writes. Tenant isolation is a later
#: layer; the reader already filters on it so a shared stream stays separable.
DEFAULT_NAMESPACE = "default"

#: The decision classes an autonomous ruling may claim.
DECISION_CLASSES = frozenset(
    {
        "high_risk_merge",
        "override_false_positive_verdict",
        "scope_widen",
        "existing_test_edit",
        "triage",
        "other",
    }
)

#: Who ruled: a human, an agent plus a notification, or an agent alone.
TIERS = frozenset({"human", "auto+notify", "auto"})

#: Every string in a record is capped, so a record cannot become a payload.
MAX_STRING = 500

#: Evidence references and alternatives must stay one-line pointers.
MAX_REFERENCE = 200

#: Names that must never appear as a key at any depth: a record is evidence,
#: not a copy of the material the decision was made from.
FORBIDDEN_NAMES = frozenset({"prompt", "payload", "secret", "token", "body"})

#: Fields the recorder owns. A caller supplying one is attempting to forge its
#: own identity or its own position in the chain, so the append is refused.
SERVER_FIELDS = (
    "actor",
    "principal_type",
    "client_id",
    "seq",
    "ts",
    "hash",
    "schema_version",
    "prev_hash",
    "namespace",
    "correlation_id",
)

#: The caller's fields: exactly these, all required, nothing else.
CALLER_FIELDS = (
    "decision_id",
    "class",
    "subject",
    "evidence",
    "rule",
    "alternatives",
    "ruling",
    "tier",
    "reversible",
    "outcome",
)

SUBJECT_FIELDS = ("plan", "story", "pr", "sha")
EVIDENCE_FIELDS = ("kind", "ref", "sha256")
RULE_FIELDS = ("layer_shas", "persona_sha", "policy_sha", "model")

_HEX_32 = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_HEX_64 = re.compile(r"\A[0-9a-fA-F]{64}\Z")


class DecisionRecordError(ValueError):
    """A decision record was refused.

    The message names the offending FIELD path (``ruling``,
    ``evidence[0].ref``, ``rule.layer_shas[1]``) and never the value it
    refused: a validator error is a log line, and a log line must not carry
    the material data minimisation exists to keep out.
    """


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


#: The decisions-stream suffix: the anchor key is ``decisions:<plan>``.
DECISIONS_SUFFIX = ".decisions.chain.jsonl"


def decision_stream_path(plan_name: str) -> Path:
    """Return the plan's decision-record stream path.

    The directory comes from :func:`pipeline.audit.audit_path`, which resolves
    the plan directory at call time rather than capturing it at import, so a
    test or a re-rooted deployment redirects this stream with the audit stream
    instead of appending to the operator's real plan directory. Going through the
    audit resolver rather than resolving the directory here keeps this module out
    of ADR 0012's shrink-only list of modules that name the plan directory
    directly: there is one resolver for "the plan's streams", and this is the
    second stream in it rather than a second resolver.
    """
    plan_key = _plan_key(plan_name)
    return audit.audit_path(plan_key).with_name(f"{plan_key}{DECISIONS_SUFFIX}")


def list_decision_chains() -> list[str]:
    """Return the plan keys that have a decision-record stream, sorted.

    The anchoring pass enumerates the decisions chains through this helper
    rather than through a glob of its own, so the plan directory is named in
    exactly one place. The directory comes from
    :func:`pipeline.audit.audit_path`, which resolves it at call time, so a
    redirected plan directory redirects this enumeration with it.

    A file name that is not an addressable plan key is skipped rather than
    returned: its anchor key would alias another chain's (see
    :func:`_plan_key`), and the caller cannot address it anyway.
    """
    probe = audit.audit_path("")
    try:
        candidates = sorted(probe.parent.glob(f"*{DECISIONS_SUFFIX}"))
    except OSError:  # pragma: no cover - a directory that cannot be listed
        return []
    plans: list[str] = []
    for path in candidates:
        plan = path.name.removesuffix(DECISIONS_SUFFIX)
        try:
            _plan_key(plan)
        except DecisionRecordError:
            # A decisions stream whose plan name is not addressable: its anchor
            # key would alias another chain's, so it is not enumerated. The
            # warning names neither the file nor the path.
            logger.warning(
                "skipping a decisions stream whose plan name is not addressable"
            )
            continue
        plans.append(plan)
    return plans


def _plan_key(plan_name: Any) -> str:
    """*plan_name* when it is a bare file name, else ``DecisionRecordError``.

    ``:`` is refused even though it is a legal character in a path: the anchored
    chain name for this stream is ``decisions:<plan>``, so a plan whose name
    already contained a colon (``decisions:other``) would produce the same anchor
    key as the decisions stream of plan ``other``. The two chains would collapse
    into one entry in the anchor set, and the surviving anchor would be compared
    against the wrong file - a permanent false "truncated" verdict on an intact
    chain, or a real cut masked. A plan name is also validated to
    ``[A-Za-z0-9._-]`` elsewhere (``pipeline.parsers._validate_key``), so no
    legitimate plan name is lost by this.
    """
    if not isinstance(plan_name, str) or not plan_name:
        raise DecisionRecordError("plan: must be a non-empty string")
    if plan_name in {".", "..", "*"} or "/" in plan_name or "\\" in plan_name:
        raise DecisionRecordError("plan: must be a bare name inside the plan directory")
    if "\x00" in plan_name or Path(plan_name).name != plan_name:
        raise DecisionRecordError("plan: must be a bare name inside the plan directory")
    if any(character < " " or character == "\x7f" for character in plan_name):
        raise DecisionRecordError("plan: must not contain control characters")
    if ":" in plan_name:
        raise DecisionRecordError(
            "plan: ':' is not allowed; it would alias another stream's anchor key"
        )
    return plan_name


def _namespace_key(namespace: Any) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise DecisionRecordError("namespace: must be a non-empty string")
    if len(namespace) > MAX_REFERENCE:
        raise DecisionRecordError("namespace: too long")
    return namespace


def _chain_head(path: Path) -> dict[str, Any] | None:
    """The last record of *path*, or ``None`` for a missing or empty stream.

    ``audit._tail`` deliberately is not used here: it returns ``None`` both for a
    missing file and for an unreadable tail, and this module must never treat the
    second as "start a new chain". An append that restarted at ``seq: 0`` after a
    corrupt line would root a second chain unlinked from the anchored one, so
    every record written after the damage would sit outside the anchor and be
    buried under an "altered" verdict for good. A tail that exists but cannot be
    read is therefore a hard error, raised before anything is written.

    Raises:
        DecisionRecordError: The last record is unparsable, is not a record, or
            carries no chain position. The message names the stream's state
            only, never the bytes it refused.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DecisionRecordError(
            f"stream: could not be read ({type(exc).__name__})"
        ) from None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise DecisionRecordError(
            "stream: the last record is unreadable; refusing to append"
        ) from None
    if not isinstance(record, dict):
        raise DecisionRecordError(
            "stream: the last record is not a record; refusing to append"
        )
    seq = record.get("seq")
    digest = record.get("hash")
    if (
        not isinstance(seq, int)
        or isinstance(seq, bool)
        or not isinstance(digest, str)
        or not digest
    ):
        raise DecisionRecordError(
            "stream: the last record carries no chain position; refusing to append"
        )
    return record


# --------------------------------------------------------------------------
# Append
# --------------------------------------------------------------------------


def append_decision_record(
    plan_name: str,
    record: dict[str, Any],
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, Any]:
    """Append one validated decision record to ``plan_name``'s chain.

    Validation (including the refusal of caller-supplied identity) runs to
    completion *before* the stream is opened, so a rejected record leaves the
    file byte-identical and consumes no ``seq``.

    The exclusive flock on the stream serializes writers, so two concurrent
    appends cannot claim the same ``seq`` or the same ``prev_hash``; the chain
    head is read from the bytes on disk under that lock rather than from a
    second "last hash" file that could drift out of step with them.

    Returns:
        The record as stored, including the fields the recorder added.

    Raises:
        DecisionRecordError: The record is malformed, carries a field the
            caller may not supply, or names a field data minimisation forbids.
    """
    payload = _validated(record)
    namespace_key = _namespace_key(namespace)
    path = decision_stream_path(plan_name)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_NOFOLLOW because a symlink planted at the stream path would redirect
        # audit evidence elsewhere; the append is the one place that can refuse it.
        fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise DecisionRecordError(
            f"stream: could not be opened ({type(exc).__name__})"
        ) from None
    try:
        # The mode passed to os.open applies only at creation; re-impose it on the
        # descriptor so a pre-existing stream with a looser mode does not stay
        # readable. This stream carries evidence references and rulings.
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        previous = _chain_head(path)
        stored: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "seq": 0 if previous is None else int(previous["seq"]) + 1,
            "prev_hash": "" if previous is None else str(previous["hash"]),
            "ts": datetime.now(UTC).isoformat(),
            "namespace": namespace_key,
            "actor": actor_id(),
            "principal_type": principal_type(),
            "client_id": client_id(),
            "correlation_id": current_correlation_id(),
            **payload,
        }
        stored["hash"] = audit._record_hash(stored)
        os.write(fd, (audit._canonical(stored) + "\n").encode("utf-8"))
        return json.loads(audit._canonical(stored))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------
# Read and verify
# --------------------------------------------------------------------------


def read_decision_records(
    plan_name: str, namespace: str = DEFAULT_NAMESPACE
) -> list[dict[str, Any]]:
    """Return ``plan_name``'s records in *namespace*, oldest first.

    A missing stream is an empty one. A corrupt line is skipped and logged by
    line number and plan - never by content - and is *not* repaired or counted
    anywhere in the result: the bytes on disk are audit evidence, so this
    reader has no side effects at all. :func:`verify_decision_chain` is the call
    that reports the damage.
    """
    namespace_key = _namespace_key(namespace)
    path = decision_stream_path(plan_name)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "skipping a corrupt decision record plan=%r line=%d", plan_name, number
            )
            continue
        if not isinstance(record, dict):
            logger.warning(
                "skipping a malformed decision record plan=%r line=%d", plan_name, number
            )
            continue
        if record.get("namespace") != namespace_key:
            continue
        records.append(record)
    return records


def verify_decision_chain(plan_name: str) -> int | None:
    """Return the ``seq`` of the first altered record, or ``None`` when intact.

    Read-only: it never re-seals or rewrites the stream, so flipping one byte
    keeps failing verification on every later call instead of being silently
    repaired. Truncation of the tail is what the anchor check adds on top.
    """
    return audit._verify_chain_at(decision_stream_path(plan_name))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validated(record: Any) -> dict[str, Any]:
    """The caller-supplied fields of *record*, validated.

    Every check raises :class:`DecisionRecordError` naming a field path only.
    """
    if not isinstance(record, dict):
        raise DecisionRecordError("record: must be a mapping")

    for name in SERVER_FIELDS:
        if name in record:
            raise DecisionRecordError(f"{name}: supplied by the recorder, not the caller")

    _reject_forbidden(record, "")

    unknown = sorted(set(record) - set(CALLER_FIELDS))
    if unknown:
        raise DecisionRecordError(f"{unknown[0]}: unknown field")

    missing = [name for name in CALLER_FIELDS if name not in record]
    if missing:
        raise DecisionRecordError(f"{missing[0]}: required field")

    decision_id = record["decision_id"]
    if not isinstance(decision_id, str) or not _HEX_32.match(decision_id):
        raise DecisionRecordError("decision_id: must be 32 hex characters")

    decision_class = record["class"]
    if decision_class not in DECISION_CLASSES:
        raise DecisionRecordError("class: not one of the recorded decision classes")

    tier = record["tier"]
    if tier not in TIERS:
        raise DecisionRecordError("tier: not one of the recorded authority tiers")
    # Who ruled is not a claim the caller gets to make. ``tier`` is the field an
    # auditor reads to decide whether a high-risk merge was human-adjudicated, so
    # an agent recording ``tier: "human"`` for a ruling it made alone is
    # misattribution in audit evidence - and self-consistent hashes would never
    # show it. The transport already says what kind of principal is recording;
    # bind the tier to it rather than trusting the caller.
    principal = principal_type()
    if tier == "human" and principal != "human":
        raise DecisionRecordError(
            "tier: a human ruling cannot be recorded by a non-human principal"
        )
    if tier == "human" and current_principal() == ANONYMOUS:
        # ANONYMOUS is a Principal, so it defaults to principal_type "human"; the
        # transport went out of its way to authenticate nobody, and such a caller
        # must never be recorded as the human who adjudicated a ruling.
        raise DecisionRecordError(
            "tier: a human ruling cannot be recorded by an unauthenticated caller"
        )

    if not isinstance(record["reversible"], bool):
        raise DecisionRecordError("reversible: must be a boolean")

    return {
        "decision_id": decision_id,
        "class": decision_class,
        "subject": _validated_subject(record["subject"]),
        "evidence": _validated_evidence(record["evidence"]),
        "rule": _validated_rule(record["rule"]),
        "alternatives": _validated_alternatives(record["alternatives"]),
        "ruling": _bounded_string(record["ruling"], "ruling", limit=MAX_STRING, allow_empty=False),
        "tier": tier,
        "reversible": record["reversible"],
        "outcome": _bounded_string(
            record["outcome"], "outcome", limit=MAX_STRING, allow_empty=True
        ),
    }


def _bounded_string(value: Any, field: str, *, limit: int, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise DecisionRecordError(f"{field}: must be a string")
    if not value and not allow_empty:
        raise DecisionRecordError(f"{field}: must not be empty")
    if len(value) > limit:
        raise DecisionRecordError(f"{field}: exceeds {limit} characters")
    return value


def _exact_keys(value: dict[str, Any], fields: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(value) - set(fields))
    if unknown:
        raise DecisionRecordError(f"{field}.{unknown[0]}: unknown field")
    missing = [name for name in fields if name not in value]
    if missing:
        raise DecisionRecordError(f"{field}.{missing[0]}: required field")


def _validated_subject(subject: Any) -> dict[str, str]:
    if not isinstance(subject, dict):
        raise DecisionRecordError("subject: must be a mapping")
    _exact_keys(subject, SUBJECT_FIELDS, "subject")
    validated = {
        name: _bounded_string(subject[name], f"subject.{name}", limit=MAX_STRING, allow_empty=True)
        for name in SUBJECT_FIELDS
    }
    for name in ("plan", "story"):
        if not validated[name]:
            raise DecisionRecordError(f"subject.{name}: must not be empty")
    return validated


def _validated_evidence(evidence: Any) -> list[dict[str, str]]:
    if not isinstance(evidence, list):
        raise DecisionRecordError("evidence: must be a list")

    validated: list[dict[str, str]] = []
    for index, entry in enumerate(evidence):
        field = f"evidence[{index}]"
        if not isinstance(entry, dict):
            raise DecisionRecordError(f"{field}: must be a mapping")
        _exact_keys(entry, EVIDENCE_FIELDS, field)
        item = {
            "kind": _bounded_string(
                entry["kind"], f"{field}.kind", limit=MAX_STRING, allow_empty=False
            ),
            "ref": _bounded_string(
                entry["ref"], f"{field}.ref", limit=MAX_REFERENCE, allow_empty=True
            ),
            "sha256": _bounded_string(
                entry["sha256"], f"{field}.sha256", limit=MAX_STRING, allow_empty=True
            ),
        }
        if item["sha256"] and not _HEX_64.match(item["sha256"]):
            raise DecisionRecordError(f"{field}.sha256: must be empty or 64 hex characters")
        validated.append(item)
    return validated


def _validated_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise DecisionRecordError("rule: must be a mapping")
    _exact_keys(rule, RULE_FIELDS, "rule")

    layer_shas = rule["layer_shas"]
    if not isinstance(layer_shas, list):
        raise DecisionRecordError("rule.layer_shas: must be a list")
    validated_shas: list[str] = []
    for index, value in enumerate(layer_shas):
        field = f"rule.layer_shas[{index}]"
        if not isinstance(value, str) or not _HEX_64.match(value):
            raise DecisionRecordError(f"{field}: must be 64 hex characters")
        validated_shas.append(value)

    return {
        "layer_shas": validated_shas,
        "persona_sha": _bounded_string(
            rule["persona_sha"], "rule.persona_sha", limit=MAX_STRING, allow_empty=True
        ),
        "policy_sha": _bounded_string(
            rule["policy_sha"], "rule.policy_sha", limit=MAX_STRING, allow_empty=True
        ),
        "model": _bounded_string(rule["model"], "rule.model", limit=MAX_STRING, allow_empty=False),
    }


def _validated_alternatives(alternatives: Any) -> list[str]:
    if not isinstance(alternatives, list):
        raise DecisionRecordError("alternatives: must be a list")
    return [
        _bounded_string(value, f"alternatives[{index}]", limit=MAX_REFERENCE, allow_empty=True)
        for index, value in enumerate(alternatives)
    ]


def _reject_forbidden(value: Any, path: str) -> None:
    """Refuse a forbidden key name anywhere below *path*.

    Recurses through mappings and lists so a prompt-shaped key cannot hide one
    level down in ``evidence[0]`` or in a nested ``subject``. The message names
    the key path only, never the value it holds.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            field = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in FORBIDDEN_NAMES:
                raise DecisionRecordError(f"{field}: forbidden field name")
            _reject_forbidden(nested, field)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden(nested, f"{path}[{index}]")
