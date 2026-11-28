"""Options for a person to choose from when a story is parked.

The overlord proposes ``park_option_count`` options drawn from the menu of
actions the park class allows (plus ``retry``), exactly one marked
recommended. Each is recorded as a ``park_options`` decision, once per
distinct ``parked_reason``. Nothing here executes an option.

``run_park_options_sweep`` never raises: it is called from the advance
wrapper without a guard.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime

from . import bound_records
from .autonomy_settings import effective
from .ci import context_from_manifest
from .overlord import _invoke_overlord, _load_policy
from .persistence import _plan_role_config
from .triage import _auto_triage_enabled, collect_triage_evidence
from .triage_park_classes import ALLOWED_ACTIONS, classify_park, with_lint_evidence

DEFAULT_OPTION_COUNT = 3
TITLE_MAX_CHARS = 120
RATIONALE_MAX_CHARS = 500
FALLBACK_RATIONALE = "overlord unavailable; default options"

_OPTION_RE = re.compile(r"^\s*OPTION\s+(\d+)\s*\|(.*)$", re.MULTILINE)
_RECOMMENDED_RE = re.compile(r"^\s*RECOMMENDED:\s*(\d+)\s*$", re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_log = logging.getLogger("pipeline")


def option_menu(park_class: str) -> list[str]:
    """The actions a person may pick for this park class, ``retry`` last."""
    allowed = ALLOWED_ACTIONS.get(park_class, ALLOWED_ACTIONS["unknown"])
    return sorted(allowed - {"park_for_human"}) + ["retry"]


def _option_count(manifest: dict) -> int:
    try:
        value = effective("park_option_count", DEFAULT_OPTION_COUNT, context_from_manifest(manifest))
    except Exception as exc:  # noqa: BLE001 - a settings read never blocks the options
        _log.warning(
            "park option count unreadable; using default (%s)",
            type(exc).__name__,
        )
        return DEFAULT_OPTION_COUNT
    if isinstance(value, int) and not isinstance(value, bool) and 2 <= value <= 5:
        return value
    return DEFAULT_OPTION_COUNT


def _clean(text: str) -> str:
    return " ".join(text.split())


def _two_sentences(text: str) -> str:
    return " ".join(_SENTENCE_END_RE.split(text)[:2])


def _parse(raw: str, menu: list[str], count: int) -> tuple[list[dict], dict[int, int]]:
    """Valid options in reply order, and a map from the reply's number to index."""
    options: list[dict] = []
    numbers: dict[int, int] = {}
    seen: set[tuple[str, str]] = set()
    for match in _OPTION_RE.finditer(raw or ""):
        parts = [_clean(p) for p in match.group(2).split("|", 3)]
        if len(parts) != 4 or parts[0] not in menu:
            continue
        action, title, rationale, diagnosis = parts
        title = title[:TITLE_MAX_CHARS]
        if not title or not rationale or (action, title) in seen:
            continue
        seen.add((action, title))
        options.append({
            "action": action,
            "title": title,
            "rationale": rationale[:RATIONALE_MAX_CHARS],
            "diagnosis": _two_sentences(diagnosis) if action == "rebrief" and diagnosis not in ("", "-") else None,
        })
        numbers.setdefault(int(match.group(1)), len(options) - 1)
        if len(options) == count:
            break
    return options, numbers


def _fallback(menu: list[str]) -> list[dict]:
    others = [a for a in menu if a != "retry"]
    actions = ["retry"] + others[:1]
    return [
        {"action": a, "title": a.replace("_", " "), "rationale": FALLBACK_RATIONALE, "diagnosis": None}
        for a in actions
    ]


def _prompt(policy: str, evidence: str, park_class: str, menu: list[str], count: int) -> str:
    return (
        f"{policy}\n\nPARK OPTIONS QUESTION: this story is parked. Propose exactly {count} "
        f"distinct options for a person to choose from.\n"
        f"EVIDENCE:\n{evidence}\n"
        f"OBSERVED PARK CLASS: {park_class}\nMENU (the only allowed actions): {', '.join(menu)}\n"
        f"Reply with one line per option, in exactly this format:\n"
        f"OPTION <n> | <action> | <title> | <rationale> | <diagnosis or ->\n"
        f"then a final line:\nRECOMMENDED: <n>\n"
        f"The diagnosis applies only to the rebrief action (at most two plain sentences); use - otherwise."
    )


def prepare_park_options(plan_name: str, story_key: str) -> dict | None:
    """Ask the overlord for options for one parked story and record them.

    Returns the record, or ``None`` when the story is missing or not parked.
    """
    from .server import _store

    manifest = bound_records.get_manifest(_store, plan_name)
    story = ((manifest or {}).get("stories") or {}).get(story_key)
    if not isinstance(story, dict) or story.get("status") != "parked":
        return None
    if manifest is None:  # unreachable: a story was found in it
        return None
    check = story.get("last_test_check")
    park_class = classify_park(
        story,
        with_lint_evidence(story, {"test_output": check.get("stdout_tail") if isinstance(check, dict) else None}),
    ).name
    menu = option_menu(park_class)
    count = _option_count(manifest)

    options: list[dict] = []
    numbers: dict[int, int] = {}
    recommended = 0
    try:
        evidence = collect_triage_evidence(story.get("worktree", ""), story, [])
        raw = _invoke_overlord(
            _prompt(_load_policy(), evidence, park_class, menu, count),
            plan_role_config=_plan_role_config(plan_name),
        )
        options, numbers = _parse(raw or "", menu, count)
        match = _RECOMMENDED_RE.search(raw or "")
        recommended = numbers.get(int(match.group(1)), 0) if match else 0
    except Exception as exc:  # noqa: BLE001 - fall back to the default options
        _log.warning("park options overlord call failed", extra={"error_type": type(exc).__name__})
    if len(options) < 2:
        options, recommended = _fallback(menu), 0
    for index, option in enumerate(options, 1):
        option["id"] = f"o{index}"
    record = {
        "kind": "park_options",
        "story_key": story_key,
        "park_class": park_class,
        "parked_reason_digest": _digest(story.get("parked_reason")),
        "options": [
            {k: o[k] for k in ("id", "action", "title", "rationale", "diagnosis")} for o in options
        ],
        "recommended": options[recommended]["id"],
        "decided_by": "overlord",
        "decided_at": datetime.now(UTC).isoformat(),
    }
    _store.append_decision(plan_name, record)
    return record


def _digest(parked_reason) -> str:
    return hashlib.sha256(str(parked_reason or "").encode()).hexdigest()[:16]


def run_park_options_sweep(plan_name: str) -> int:
    """Prepare options for at most one parked story that has none for its
    current ``parked_reason``, while failure triage is enabled. Returns how many were prepared; never raises."""
    try:
        if not _auto_triage_enabled():
            return 0
        from .server import _store

        manifest = bound_records.get_manifest(_store, plan_name)
        if not manifest or manifest.get("paused"):
            return 0
        prepared = {
            (r.get("story_key"), r.get("parked_reason_digest"))
            for r in _store.get_decisions(plan_name)
            if isinstance(r, dict) and r.get("kind") == "park_options"
        }
        for key, story in sorted((manifest.get("stories") or {}).items()):
            if not isinstance(story, dict) or story.get("status") != "parked":
                continue
            if (key, _digest(story.get("parked_reason"))) in prepared:
                continue
            return 1 if prepare_park_options(plan_name, key) else 0
        return 0
    except Exception as exc:  # noqa: BLE001 - the tick must survive this sweep
        _log.warning("park options sweep raised", extra={"error_type": type(exc).__name__})
        return 0
