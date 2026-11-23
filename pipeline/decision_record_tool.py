"""``record_decision``: record a caller's judgment call in the plan's chain.

An autonomous ruling (a high-risk merge, a false-positive override, a scope
widening, a triage verdict) is evidence only if it lands in the plan's
hash-chained decision stream next to the material it was made from. This module
is the MCP tool that does it: it authorizes the caller, validates the ruling at
the trust boundary, rate-limits it per principal, appends one record to the
plan's existing v2 stream, and writes one audit record naming the decision id.

What the record carries, and why (all of it is a v2 schema decision, not this
tool's):

* **The rationale is an ``alternatives`` entry.** The v2 record has no
  rationale field, so the rationale is stored as the single element
  ``"rationale: <text>"`` of ``alternatives``. A dedicated field is a later S3
  revision, not this story. Because the v2 validator caps one ``alternatives``
  entry at 200 characters, the text after the ``"rationale: "`` prefix is
  truncated to 189 characters (200 minus the 11-character prefix), so the
  stored entry is exactly at most 200 characters.
* **The question is not persisted.** The v2 record has no field for it, and
  adding one would be an unknown field the validator refuses, so the argument is
  accepted (a later S3 revision can store it) and deliberately not echoed
  anywhere -- not into the record, not into the audit trail.
* **``tier`` follows the authenticated principal, never an argument.** The v2
  chain binds ``tier`` to the transport identity: it refuses ``tier="human"``
  from a non-human principal, because an auditor reads ``tier`` to decide
  whether a high-risk merge was human-adjudicated, and an agent recording
  ``human`` for its own ruling would be misattribution that self-consistent
  hashes can never reveal. So a human caller (every call under the shared-secret
  transport, where nothing is bound and the caller resolves to the local
  operator) records ``tier="human"``, and an agent principal records
  ``tier="auto"`` -- an agent ruling alone, which is what actually happened. An
  agent-recorded judgment is a *record*, not an authority grant: nothing here
  widens what the agent may do next.
* **Identity comes from the bound principal (S2), never from an argument.** A
  caller passing ``actor="human:bob"`` changes nothing: ``pipeline.actor``
  stamps the record from ``current_principal()``.
* **Rate limiting is in-process and per principal subject.** The sliding
  one-minute window lives in this module's memory, so it resets when the
  process restarts, and it is not shared with a second server process. The limit
  is ``PIPELINE_RECORD_DECISION_PER_MINUTE`` (default 30); a missing, malformed
  (``"abc"``) or non-positive (``"0"``, ``"-5"``) value means 30 rather than
  "allow nothing".

Limited attribution, stated honestly: under the shared-secret provider the
caller is ``LOCAL_OPERATOR``, so a record cannot distinguish an agent session
from a human one. Agent attribution needs the OIDC identity seam (S2).
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from collections import deque
from typing import Any

from app.policy import Action

from . import authorization
from .actor import current_principal
from .audit import record_audit
from .decision_records import (
    DECISION_CLASSES,
    DecisionRecordError,
    append_decision_record,
)

logger = logging.getLogger("pipeline.decision_record_tool")

#: The tool's action label, spelled once so the policy grant, the audit record
#: and the authorization check cannot drift apart.
ACTION = Action.DECISION_RECORD

#: Rolling one-minute window limit for one principal subject.
RATE_ENV = "PIPELINE_RECORD_DECISION_PER_MINUTE"
DEFAULT_PER_MINUTE = 30

#: The host-wide model recorded in ``rule.model`` when the caller's model is not
#: known to the server. ``rule.model`` must be a non-empty string in v2.
MODEL_ENV = "PIPELINE_RECORD_DECISION_MODEL"
UNKNOWN_MODEL = "unspecified"

#: Prefix that marks the rationale's home in ``alternatives``.
RATIONALE_PREFIX = "rationale: "

#: v2 caps one ``alternatives`` entry at this many characters.
ALTERNATIVES_LIMIT = 200

#: v2 caps ``ruling`` and ``evidence[i].kind`` at this many characters.
RULING_LIMIT = 500

#: v2 caps ``evidence[i].ref`` at this many characters.
REFERENCE_LIMIT = 200

#: Seconds in the rate-limit window.
WINDOW_SECONDS = 60.0

#: Accepted times, keyed by principal subject: the sliding window itself.
_WINDOWS: dict[str, deque[float]] = {}


def _now() -> float:
    """Return the monotonic clock, through one seam a test can replace.

    Monotonic, not wall clock: a rate limit keyed on ``time.time()`` moves
    whenever the host's clock is stepped, which would either refuse a caller for
    an hour or let it past the limit.
    """
    return time.monotonic()


def _per_minute() -> int:
    """Return the configured per-minute limit; anything unusable means 30."""
    raw = os.environ.get(RATE_ENV)
    if raw is None:
        return DEFAULT_PER_MINUTE
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PER_MINUTE
    return value if value > 0 else DEFAULT_PER_MINUTE


def _limit_refusal(subject: str) -> dict[str, Any] | None:
    """Return a ``rate_limited`` refusal, or admit the call and count it.

    Only an admitted call is appended: a refusal must not consume a slot, or a
    caller that retries after the advertised delay would still be refused and
    the window would never drain.
    """
    limit = _per_minute()
    now = _now()
    window = _WINDOWS.setdefault(subject, deque())

    while window and window[0] <= now - WINDOW_SECONDS:
        window.popleft()

    if len(window) >= limit:
        retry_after = max(1, math.ceil(window[0] + WINDOW_SECONDS - now))
        return {
            "recorded": False,
            "error": "rate_limited",
            "retry_after_seconds": retry_after,
        }

    window.append(now)
    return None


def _text(value: Any) -> str:
    """Return *value* stripped when it is a string, else ``""``."""
    return value.strip() if isinstance(value, str) else ""


def _bounded(value: str, limit: int) -> str:
    """Return *value* truncated to *limit* characters."""
    return value[:limit]


def _valid_evidence(evidence: Any) -> list[dict[str, str]] | None:
    """Return the evidence normalized for the v2 record, or ``None``.

    At least one entry must be a mapping with a non-empty ``kind`` and ``ref``;
    anything else in the list is ignored rather than refused, so one malformed
    item does not discard a pointer the caller did supply. Extra keys are
    dropped: the v2 record accepts exactly ``kind``/``ref``/``sha256``.
    """
    if not isinstance(evidence, list):
        return None

    validated: list[dict[str, str]] = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        kind = _text(entry.get("kind"))
        ref = _text(entry.get("ref"))
        if not kind or not ref:
            continue
        sha256 = _text(entry.get("sha256"))
        validated.append(
            {
                "kind": _bounded(kind, RULING_LIMIT),
                "ref": _bounded(ref, REFERENCE_LIMIT),
                "sha256": sha256,
            }
        )
    return validated or None


def _rationale_entry(rationale: str) -> str:
    """Return the ``alternatives`` entry that carries *rationale*.

    The prefix is part of the capped budget, so the text keeps
    ``ALTERNATIVES_LIMIT - len(RATIONALE_PREFIX)`` characters; truncating the
    prefixed string instead would silently drop characters of the rationale.
    """
    room = max(0, ALTERNATIVES_LIMIT - len(RATIONALE_PREFIX))
    return RATIONALE_PREFIX + rationale[:room]


def _manifest(plan_name: str) -> dict[str, Any]:
    """Return the plan manifest through the fail-closed loader.

    The same helper ``authorization.authorize`` uses, so the story check and the
    authorization decision read one manifest: a hidden plan and a missing plan
    cannot disagree about what exists.
    """
    return authorization._load_manifest(plan_name)


def _record_tier(principal: Any) -> str:
    """Return the ``tier`` the bound principal may record.

    A human records a human ruling. An agent records ``auto`` -- an agent ruling
    alone -- because the v2 chain refuses ``tier="human"`` from a non-human
    principal, and claiming otherwise would be misattribution in audit evidence.
    """
    return "human" if getattr(principal, "principal_type", "") == "human" else "auto"


def _audit(plan_name: str, story: str, outcome: str, decision_id: str | None) -> None:
    """Write the one audit record for one attempt.

    Only the decision id and the story key go in: a ruling, a rationale, a
    question or an evidence reference is material the audit chain must not
    carry, and ``record_audit`` already refuses to fail the caller's action.
    """
    record_audit(
        plan_name,
        action=ACTION.value,
        outcome=outcome,
        target={"story": story},
        detail={"decision_id": decision_id},
    )


def _refusal(
    plan_name: str, story: str, error: str, **extra: Any
) -> dict[str, Any]:
    """Audit and return one refusal.

    The reply names a fixed code and nothing else -- no argument value, no plan
    or story content -- so a refused call cannot be used to echo material back
    out of the server.
    """
    _audit(plan_name, story, "refused", None)
    return {"recorded": False, "error": error, **extra}


def record_decision(
    plan_name: str,
    story_key: str,
    decision_class: str,
    question: str,
    ruling: str,
    rationale: str,
    evidence: Any,
    reversible: bool = True,
    **ignored: Any,
) -> dict[str, Any]:
    """Record one judgment call in ``plan_name``'s hash-chained stream.

    Authorizes ``decision:record`` on the plan first (a plan the caller may not
    see is refused exactly as a plan that does not exist is), then validates the
    ruling, the rationale, the evidence, the decision class and the story key,
    then applies the per-principal rate limit, then appends one record and
    writes one audit record.

    Refusals never raise and return ``{"recorded": false, "error": <code>}``
    with one of ``not_authorized``, ``empty_ruling``, ``empty_rationale``,
    ``empty_evidence``, ``invalid_decision_class``, ``unknown_story``,
    ``rate_limited`` (plus ``retry_after_seconds``) or ``record_refused`` (the
    v2 validator refused a record that passed the checks above -- defence in
    depth, never expected).

    Success returns ``{"recorded": true, "decision_id": <32 hex>, "seq": <int>}``.

    The record: ``tier`` follows the bound principal (``human`` for a human,
    ``auto`` for an agent -- the v2 chain refuses ``tier="human"`` from a
    non-human principal, so "always human" would be misattribution); the
    caller's identity is stamped from the bound principal (S2) and never from an
    argument, so an ``actor`` argument is ignored; ``rule`` carries empty layer,
    persona and policy shas and ``PIPELINE_RECORD_DECISION_MODEL`` (or
    ``"unspecified"``); ``alternatives`` carries the rationale as its single
    element ``"rationale: <text>"`` (an ``alternatives`` entry is capped at 200
    characters, so the text keeps 189 of them -- a dedicated rationale field is a
    later S3 revision); ``outcome`` is empty. The ``question`` is accepted and
    not persisted: the v2 record has no field for it.

    Rate limiting: ``PIPELINE_RECORD_DECISION_PER_MINUTE`` calls per minute per
    principal subject, default 30; a malformed or non-positive value means 30.
    The window is in-process memory, so it resets when the process restarts and
    is not shared between processes.

    Attribution under the shared-secret provider: the caller resolves to
    ``LOCAL_OPERATOR``, so a record cannot tell an agent session from a human
    one. Agent attribution needs the OIDC identity seam (S2).
    """
    principal = current_principal()
    subject = getattr(principal, "subject", "")

    # Authorization first, as the story requires: nothing below may learn
    # whether a plan exists before the policy has decided the caller may act on
    # it. A missing plan and a hidden plan both reach the same refusal, because
    # the manifest this loads is the one the authorizer reads.
    if not isinstance(plan_name, str) or not plan_name.strip():
        return {"recorded": False, "error": "not_authorized"}
    plan_name = plan_name.strip()

    denial = authorization.authorize(
        ACTION,
        plan_name=plan_name,
        manifest=_manifest(plan_name),
        record_allowed=False,
    )
    if denial is not None:
        # authorize already recorded the denial in the plan's audit chain and
        # sidecar; a second "refused" record would say the same thing twice.
        return {"recorded": False, "error": "not_authorized"}

    story = story_key.strip() if isinstance(story_key, str) else ""

    if not _text(ruling):
        return _refusal(plan_name, story, "empty_ruling")
    if not _text(rationale):
        return _refusal(plan_name, story, "empty_rationale")

    evidence_items = _valid_evidence(evidence)
    if evidence_items is None:
        return _refusal(plan_name, story, "empty_evidence")

    if not isinstance(decision_class, str) or decision_class not in DECISION_CLASSES:
        return _refusal(plan_name, story, "invalid_decision_class")

    if not isinstance(story_key, str) or not story:
        return _refusal(plan_name, story, "unknown_story")
    stories = _manifest(plan_name).get("stories")
    stories = stories if isinstance(stories, dict) else {}
    if story not in stories:
        return _refusal(plan_name, story, "unknown_story")

    limited = _limit_refusal(subject)
    if limited is not None:
        _audit(plan_name, story, "refused", None)
        return limited

    decision_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "decision_id": decision_id,
        "class": decision_class,
        "subject": {"plan": plan_name, "story": story, "pr": "", "sha": ""},
        "evidence": evidence_items,
        "rule": {
            "layer_shas": [],
            "persona_sha": "",
            "policy_sha": "",
            "model": _bounded(_text(os.environ.get(MODEL_ENV)) or UNKNOWN_MODEL, 500),
        },
        "alternatives": [_rationale_entry(_text(rationale))],
        "ruling": _bounded(_text(ruling), RULING_LIMIT),
        "tier": _record_tier(principal),
        "reversible": reversible if isinstance(reversible, bool) else True,
        "outcome": "",
    }

    try:
        stored = append_decision_record(plan_name, record)
    except (DecisionRecordError, OSError) as exc:
        # Never the message: the validator names fields, but an OSError can
        # carry a path, and neither belongs in a log line about a refused write.
        logger.warning(
            "decision record refused plan=%s error=%s",
            plan_name,
            type(exc).__name__,
        )
        return _refusal(plan_name, story, "record_refused")

    stored_id = stored.get("decision_id") if isinstance(stored, dict) else None
    returned_id = stored_id if isinstance(stored_id, str) and stored_id else decision_id
    seq = stored.get("seq") if isinstance(stored, dict) else None

    _audit(plan_name, story, "recorded", returned_id)
    return {"recorded": True, "decision_id": returned_id, "seq": seq}
