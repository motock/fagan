"""External dependency references in ``blocked_by``.

``ref:<namespace>:<plan>:<story_key>`` names a story that lives in another
namespace. Core never resolves one on its own: ingest asks the reference hook
(``pipeline.access_hooks.reference_refusal``) whether the current namespace may
depend on it, and readiness reads only the referenced story's status string.
Nothing here writes to another namespace.

With no reference plugin selected every such entry is refused. Plain
``<plan>:<story_key>`` entries are left to the existing check, unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from pipeline import access_hooks
from pipeline.actor import current_namespace
from pipeline.store_records import InvalidRecordKeyError, validate_namespace

logger = logging.getLogger("pipeline")

REF_PREFIX = "ref:"
UNKNOWN_STATUS = "unknown"

_SATISFIED_STATUSES = frozenset({"done", "merged"})
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MALFORMED = (
    "`blocked_by` external reference must be 'ref:<namespace>:<plan>:<story_key>'"
)
_REFUSED = "`blocked_by` external reference is refused by the reference hook"


def is_ref(entry: Any) -> bool:
    """Return True when ``entry`` is an external reference (not validated)."""
    return isinstance(entry, str) and entry.startswith(REF_PREFIX)


def parse_ref(entry: Any) -> tuple[str, str, str] | None:
    """Return ``(namespace, plan, story_key)`` for a well-formed reference, else None.

    The namespace is the one-segment or two-segment form the store accepts. The
    plan and key are flat names of the same shape as a story key. Three colon
    separated parts are required, so a colon inside a part is malformed.
    """
    if not is_ref(entry):
        return None
    parts = entry[len(REF_PREFIX):].split(":")
    if len(parts) != 3:
        return None
    namespace, plan, key = parts
    if _NAME_RE.fullmatch(plan) is None or _NAME_RE.fullmatch(key) is None:
        return None
    try:
        validate_namespace(namespace)
    except InvalidRecordKeyError:
        return None
    return namespace, plan, key


def blocked_by_error(
    story: dict[str, Any], plain_check: Callable[[dict[str, Any]], str | None]
) -> str | None:
    """Return the ``blocked_by`` error for ``story``, or None when it is valid.

    Plain entries go through ``plain_check`` exactly as before, so a story with
    no reference entry gets the same result it always did. Each reference must
    be well-formed and allowed by the reference hook for the current namespace.
    A refused reference is never echoed: the error names only the rule.
    """
    blocked_by = story.get("blocked_by")
    if not isinstance(blocked_by, list):
        return plain_check(story)
    plain_error = plain_check(
        {**story, "blocked_by": [entry for entry in blocked_by if not is_ref(entry)]}
    )
    if plain_error is not None:
        return plain_error
    for entry in blocked_by:
        if is_ref(entry):
            reference_error = _reference_error(entry)
            if reference_error is not None:
                return reference_error
    return None


def _reference_error(entry: str) -> str | None:
    parsed = parse_ref(entry)
    if parsed is None:
        return _MALFORMED
    namespace, _plan, _key = parsed
    if access_hooks.reference_refusal(current_namespace(), namespace) is not None:
        return _REFUSED
    return None


def status_of(store: Any, namespace: str, plan: str, story_key: str) -> str:
    """Return the referenced story's status string, or ``unknown``.

    This is the only read readiness makes of another namespace. It returns the
    status and nothing else, and any failure to read (an unserved namespace, a
    missing plan or story, a non-string status) is ``unknown``, which holds.
    """
    try:
        manifest = store.records().get_manifest(namespace, plan)
        stories = (manifest or {}).get("stories")
        story = stories.get(story_key) if isinstance(stories, dict) else None
        status = story.get("status") if isinstance(story, dict) else None
    except Exception as exc:  # noqa: BLE001 - an unreadable reference holds, never releases
        logger.warning(
            "external reference unreadable; story holds as unknown (%s)",
            type(exc).__name__,
        )
        return UNKNOWN_STATUS
    return status if isinstance(status, str) else UNKNOWN_STATUS


def external_gaps(store: Any, story: dict[str, Any]) -> list[str]:
    """Return the external references of ``story`` that do not release it yet.

    A reference holds the story unless the reference hook allows it right now
    and the referenced story is done or merged. A malformed reference holds.
    Plain entries are not this function's concern and are never returned.
    """
    gaps = []
    for entry in story.get("blocked_by") or []:
        if not is_ref(entry):
            continue
        parsed = parse_ref(entry)
        if parsed is None:
            gaps.append(entry)
            continue
        namespace, plan, key = parsed
        allowed = access_hooks.reference_allowed(current_namespace(), namespace)
        if not allowed or status_of(store, namespace, plan, key) not in _SATISFIED_STATUSES:
            gaps.append(entry)
    return gaps
