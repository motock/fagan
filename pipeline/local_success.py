"""LD90-W0-06: pure first-pass-clean classifier for local (non-Claude) stories.

This module implements the logic used by the 90% first-pass-clean metric.  It
provides two public functions:

* :func:`classify_story` – classify a single story against its records.
* :func:`rolling_rate` – compute a rolling window rate from a list of
  classified stories.

The implementation is intentionally pure and stdlib‑only.  No I/O, no
dependencies on other parts of the pipeline.

The definitions below match the recorded baseline method and the unit tests.
They include population membership, clean status, tier resolution, and reason
codes for legacy messages, escalations, and brief rewrite markers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

__all__ = ["classify_story", "rolling_rate"]

# Regular expressions used by the classifier.
_RE_LEGACY_MESSAGE = re.compile(r"escalat|parked|fallback|triage|wedge", re.IGNORECASE)
_RE_BRIEF_REWRITE = re.compile(r"(?<![A-Za-z_])(REWORK|AMENDMENT)(?![A-Za-z_])")

# Helper to find records that belong to a story.

def _matched_records(story_key: str, story: dict, records: Iterable[dict]) -> list[dict]:
    """Return a list of records that match the given story.

    A record matches if any of the following is true:

    * ``record.get("story_key") == story_key``
    * The story has a truthy ``correlation_id`` and the record's
      ``correlation_id`` equals it.
    * The record's ``message`` (defaulting to ``""``) starts with
      ``story_key + " "`` or ``"story " + story_key + " "``.
    """
    matched: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("story_key") == story_key:
            matched.append(rec)
            continue
        corr = story.get("correlation_id")
        if corr and rec.get("correlation_id") == corr:
            matched.append(rec)
            continue
        msg = str(rec.get("message", ""))
        if msg.startswith((f"{story_key} ", f"story {story_key} ")):
            matched.append(rec)
    return matched


def classify_story(story_key: str, story: dict, records: list[dict]) -> dict:
    """Classify a story and return a dict with the required keys.

    The returned dict contains the keys ``story_key``, ``in_population``,
    ``tier``, ``dispatched_at``, ``clean`` and ``reasons``.  ``reasons`` is a
    sorted list of strings, each reason appearing at most once; it is empty iff ``clean`` is ``True``.
    """
    matched = _matched_records(story_key, story, records)

    # Population and tier attribution both describe the tier the story was
    # FIRST dispatched on: escalation overwrites backend / model /
    # dispatched_model with the escalation target, so without the
    # pre-escalation stamp (pipeline/escalation.py::_stamp_first_dispatch) a
    # story that failed on the local tier and was escalated would be counted
    # as an escalation-tier story.
    backend = story.get("pre_escalation_backend") or story.get("backend")
    tag = (
        story.get("pre_escalation_model")
        or story.get("dispatched_model")
        or story.get("model")
        or ""
    )
    escalated_flag = story.get("escalated") is True
    any_escalated_event = any(rec.get("event") in {"escalated", "model_fallback"} for rec in matched)
    # A manifest written before the dispatch path stamped ``backend`` carries
    # only the model tag; a non-cloud tag is still a local dispatch, so that
    # story belongs to the population too.
    in_population = (
        bool(backend and backend != "claude")
        or escalated_flag
        or any_escalated_event
        or (not backend and bool(tag))
    )

    # Tier determination.
    if tag.endswith(":cloud"):
        tier = "cloud-oss"
    elif tag:
        # If backend is claude without any pre-escalation info, ignore tag
        if backend == "claude" and not story.get("pre_escalation_backend") and not story.get("pre_escalation_model"):
            tier = "unknown"
        else:
            tier = "on-device"
    elif backend and backend != "claude":
        tier = "on-device"
    elif backend:
        tier = "unknown"
    else:
        tier = "unknown"

    dispatched_at = story.get("dispatched_at")

    # Clean determination.
    reasons: set[str] = set()
    clean = True

    if story.get("status") != "done":
        reasons.add("not_done")
        clean = False

    if escalated_flag:
        # The manifest flag is the durable record of an escalation; the
        # sidecar event is not always present.
        reasons.add("escalated")
        clean = False

    for rec in matched:
        event = rec.get("event")
        if event in {"escalated", "model_fallback", "story_parked", "brief_patched"}:
            reasons.add(event)
            clean = False
        elif event is None:
            msg = str(rec.get("message", ""))
            if _RE_LEGACY_MESSAGE.search(msg):
                reasons.add("legacy_message")
                clean = False

    instr = str(story.get("agent_instructions", ""))
    if _RE_BRIEF_REWRITE.search(instr):
        reasons.add("brief_rewrite_marker")
        clean = False

    

    return {
        "story_key": story_key,
        "in_population": in_population,
        "tier": tier,
        "dispatched_at": dispatched_at,
        "clean": clean,
        "reasons": sorted(reasons),
    }


def rolling_rate(classified: Sequence[dict], window: int = 30, tier: str | None = None) -> dict:
    """Compute a rolling window rate from classified stories.

    Only entries that are in the population, have a non-empty ``dispatched_at``
    and match the optional ``tier`` filter are considered.  The entries are
    sorted by ``dispatched_at`` ascending; the last ``window`` entries are
    kept (or all if ``window <= 0``).  The result contains ``count``, ``clean``,
    ``rate`` (rounded to three decimal places or ``None`` if ``count`` is 0)
    and a ``reasons`` mapping from reason string to its count.
    """
    # Filter entries.
    filtered = [e for e in classified if e.get("in_population") and e.get("dispatched_at")]
    if tier is not None:
        filtered = [e for e in filtered if e.get("tier") == tier]

    # Sort by dispatched_at.
    ordered = sorted(filtered, key=lambda e: e["dispatched_at"])

    # Apply window.
    if window <= 0:
        kept = ordered
    else:
        kept = ordered[-window:]

    count = len(kept)
    clean_count = sum(1 for e in kept if e.get("clean"))
    rate = round(clean_count / count, 3) if count else None

    # Aggregate reasons.
    reasons: dict[str, int] = {}
    for e in kept:
        for r in e.get("reasons", []):
            reasons[r] = reasons.get(r, 0) + 1

    return {"count": count, "clean": clean_count, "rate": rate, "reasons": reasons}
