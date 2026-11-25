"""Recent human answers to the same kind of decision, offered to the overlord as precedent.

The overlord rules each parked story and each high-risk merge from the policy,
the authority matrix and the story's evidence. It never sees how people
answered the same kind of decision before. This module reads those answers
back: for one park class (or ``high_risk_merge``) it pairs each overlord ruling
with the ``human resolution`` that answers it, and returns the newest few,
disagreements first.

The block is guidance. The policy and the authority matrix still decide what is
allowed; nothing here grants an action.

Namespace: only the namespace bound for the call is read, so a plan in another
namespace never reaches a ruling.

Cost: the store's decision reads are per plan and take no limit, so a
cross-plan read cannot be bounded at the store. The paired rulings of a
namespace are read once and reused for ``PRECEDENT_CACHE_SECONDS``; every
ruling inside that window shares the one read.
"""

from __future__ import annotations

import time
from typing import Any

from . import bound_records, decision_log
from . import context_sources as ctxsrc
from .actor import current_namespace
from .autonomy_settings import effective
from .ci import context_from_manifest
from .store_records import DEFAULT_NAMESPACE
from .triage_lint_evidence import with_lint_evidence
from .triage_outcomes import HIGH_RISK_MERGE_CLASS, RATIONALE_LIMIT
from .triage_park_classes import classify_park
from .triage_readiness import PARK_ACTION

SETTING_KEY = "overlord_precedent_limit"
DEFAULT_LIMIT = 8
PRECEDENT_CACHE_SECONDS = 60.0

_TRIAGE_QUESTION = "failure triage"
_ADJUDICATION_QUESTION = "high-risk merge adjudication"
_RESOLUTION_QUESTION = "human resolution"
_MERGE_PROCEED = "proceed"
_MERGE_PARK = "park"
_HUMAN_PARK = "park"
_HUMAN_MERGE_HELD = "merge_held"
_HUMAN_INPUT_ACTIONS = frozenset({"amend_scope", "rebrief", "approve_test_edit"})

_GUIDANCE_HEADER = (
    "PRECEDENT (how people answered the same kind of decision before; guidance only: "
    "the policy and the authority matrix still decide what is allowed):"
)

_clock = time.monotonic
#: Namespace -> (store read from, read at, paired rulings newest first). One read per window.
_cache: dict[str, tuple[Any, float, list[dict[str, Any]]]] = {}


def clear_cache() -> None:
    """Forget every namespace's paired rulings, so the next read goes to the store."""
    _cache.clear()


def observed_park_class(story: dict) -> str:
    """Return the park class triage observes for ``story``, the same call ``rule_on_story`` makes."""
    check = story.get("last_test_check")
    evidence = {"test_output": check.get("stdout_tail") if isinstance(check, dict) else None}
    return classify_park(story, with_lint_evidence(story, evidence)).name


def class_for_phase(phase: str, story: dict) -> str:
    """Return the park class whose precedent a ``phase`` ruling on ``story`` is shown."""
    if phase == "merge_adjudication":
        return HIGH_RISK_MERGE_CLASS
    return observed_park_class(story)


def configured_limit(manifest: dict) -> int:
    """Return ``overlord_precedent_limit`` for the plan named by ``manifest``; 0 disables."""
    value = effective(SETTING_KEY, DEFAULT_LIMIT, context_from_manifest(manifest))
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_LIMIT


def pair_rulings(plan_name: str, records: list[dict]) -> list[dict]:
    """Pair each overlord ruling in ``records`` with the human resolution that answers it.

    A ruling pairs with the latest resolution for the same story whose
    ``ruling_ref`` is the ruling's ``decided_at``. A failed-open triage and an
    unparseable merge adjudication ruled nothing, so they never pair.
    """
    resolutions: dict[tuple[Any, str], dict[str, Any]] = {}
    for record in records:
        ref = record.get("ruling_ref")
        if record.get("question") == _RESOLUTION_QUESTION and isinstance(ref, str):
            resolutions[(record.get("story_key"), ref)] = record
    pairs = []
    for ruling in records:
        ruled = _ruled(ruling)
        if ruled is None:
            continue
        park_class, action = ruled
        decided_at = ruling["decided_at"]
        resolution = resolutions.get((ruling.get("story_key"), decided_at))
        if resolution is None:
            continue
        rationale = resolution.get("rationale")
        pairs.append(
            {
                "plan": plan_name,
                "story_key": ruling.get("story_key"),
                "park_class": park_class,
                "overlord_action": action,
                "human_action": resolution.get("human_action"),
                "agree": _agrees(park_class, action, resolution),
                "rationale": rationale[:RATIONALE_LIMIT] if isinstance(rationale, str) else "",
                "decided_at": decided_at,
            }
        )
    return pairs


def precedent_entries(park_class: str, limit: int) -> list[dict]:
    """Return up to ``limit`` newest paired rulings of ``park_class`` in the bound namespace.

    The newest ``limit`` are chosen first, then disagreements are listed before
    agreements, each group still newest first. Reads nothing when ``limit`` is 0.
    """
    if limit <= 0:
        return []
    matching = [
        pair for pair in _namespace_pairs(current_namespace()) if pair["park_class"] == park_class
    ][:limit]
    return sorted(matching, key=lambda pair: pair["agree"])


def render_precedent(entries: list[dict]) -> str:
    """Render ``entries`` as the guidance block: a header, then one line per answer."""
    lines = [_GUIDANCE_HEADER]
    for entry in entries:
        verdict = "agree" if entry["agree"] else "disagree"
        rationale = " ".join(entry["rationale"].split()) or "(no rationale recorded)"
        lines.append(
            f"- plan {entry['plan']} story {entry['story_key']}: overlord {entry['overlord_action']}, "
            f"human {entry['human_action']} ({verdict}). Rationale: {rationale}"
        )
    return "\n".join(lines)


def precedent_block(park_class: str, limit: int) -> tuple[str, str | None]:
    """Return ``(block, digest)`` for ``park_class``, or ``("", None)`` when there is none.

    The block is stored as a context snapshot, so ``digest`` names exactly the
    text the overlord was shown and replay can load it.
    """
    entries = precedent_entries(park_class, limit)
    if not entries:
        return "", None
    text = render_precedent(entries)
    return text, ctxsrc.snapshot_context(text)


def _ruled(record: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(park_class, overlord action)`` when ``record`` is a ruling that can pair."""
    decided_at = record.get("decided_at")
    if not isinstance(decided_at, str):
        return None
    question = record.get("question")
    if question == _TRIAGE_QUESTION:
        park_class, action = record.get("park_class"), record.get("action")
        if record.get("failed_open") or not (isinstance(park_class, str) and isinstance(action, str)):
            return None
        return (park_class, action) if park_class and action else None
    if question == _ADJUDICATION_QUESTION and record.get("ruling") in (_MERGE_PROCEED, _MERGE_PARK):
        return HIGH_RISK_MERGE_CLASS, record["ruling"]
    return None


def _agrees(park_class: str, action: str, resolution: dict[str, Any]) -> bool:
    """Judge a pairing the way ``triage_readiness`` scores it.

    A park the human answered with park, or with input the machine did not
    have, agrees. A proceed the human held is agreement for a merge.
    """
    human_action = resolution.get("human_action")
    if park_class == HIGH_RISK_MERGE_CLASS:
        held = human_action == _HUMAN_MERGE_HELD
        return held if action == _MERGE_PROCEED else not held
    if action == PARK_ACTION:
        return human_action == _HUMAN_PARK or _supplied_input(resolution)
    return human_action != _HUMAN_PARK


def _supplied_input(resolution: dict[str, Any]) -> bool:
    return (
        resolution.get("human_action") in _HUMAN_INPUT_ACTIONS
        or bool(resolution.get("files_added"))
        or resolution.get("brief_changed") is True
    )


def _namespace_pairs(namespace: str) -> list[dict[str, Any]]:
    """Return every paired ruling in ``namespace``, newest first, reusing a read inside the window.

    A read is reused only while the store it was taken from is still the
    active one, so a swapped store is never answered from the old store's rows.
    """
    store = _active_store()
    now = _clock()
    cached = _cache.get(namespace)
    if cached is not None and cached[0] is store and now - cached[1] < PRECEDENT_CACHE_SECONDS:
        return cached[2]
    pairs: list[dict[str, Any]] = []
    for plan_name, records in _plan_decisions(store):
        pairs.extend(pair_rulings(plan_name, records))
    pairs.sort(key=lambda pair: pair["decided_at"], reverse=True)
    _cache[namespace] = (store, now, pairs)
    return pairs


def _active_store() -> Any:
    """Return the server's configured record store."""
    # Imported here: server loads the modules that call this one.
    from .server import _store

    return _store


def _plan_decisions(store: Any) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return ``(plan, decision records)`` for every plan in the bound namespace."""
    if decision_log.store_owns_records():
        plans = bound_records.list_plans(store)
        return [(plan, bound_records.get_decisions(store, plan) or []) for plan in plans]
    # The file layout holds only the default namespace.
    if current_namespace() != DEFAULT_NAMESPACE:
        return []
    return [(plan, decision_log.read_decisions(plan)) for plan in store.list_manifests()]
