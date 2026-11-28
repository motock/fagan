"""Pure helpers for the triage sweep, split out of ``pipeline.triage``.

Every name stays importable from ``pipeline.triage``.
"""

import os
from datetime import UTC, datetime


def _merge_sweep_changes(on_disk: dict, initial: dict, current: dict) -> dict:
    """Apply only the sweep's own mutations (initial -> current) onto *on_disk*.

    The sweep holds its snapshot across minutes-long LLM rulings, so writing
    it wholesale would revert anything written to the file meanwhile. A story
    absent from disk (dropped by a concurrent re-ingest) is not resurrected.
    """
    for field, value in current.items():
        if field != "stories" and initial.get(field) != value:
            on_disk[field] = value
    disk_stories = on_disk.setdefault("stories", {})
    initial_stories = initial.get("stories", {})
    for key, story in current.get("stories", {}).items():
        if key not in initial_stories:
            disk_stories[key] = story
            continue
        if key not in disk_stories or initial_stories[key] == story:
            continue
        for field, value in story.items():
            if initial_stories[key].get(field) != value:
                disk_stories[key][field] = value
        for field in initial_stories[key].keys() - story.keys():
            disk_stories[key].pop(field, None)
    return on_disk


def _merge_hold_is_stale(story: dict) -> bool:
    """True once a merge-gate hold is older than the triage grace window.

    Fail-secure: a missing, unparseable, or future ``merge_parked_at`` is
    never stale, so a legacy park keeps today's exclusion exactly.
    """
    ts = story.get("merge_parked_at")
    if not isinstance(ts, str):
        return False
    try:
        parked_at = datetime.fromisoformat(ts)
    except ValueError:
        return False
    try:
        grace = int(os.environ.get("PIPELINE_TRIAGE_MERGE_HOLD_GRACE_SECONDS", "86400"))
    except ValueError:
        grace = 86400
    if grace < 0:
        grace = 86400
    try:
        age = (datetime.now(UTC) - parked_at).total_seconds()
    except TypeError:
        return False
    return age > grace


def _exception_type_chain(exc: BaseException) -> str:
    """Return the class names along ``exc``'s cause/context chain, outermost
    first, joined by ``" <- "`` (at most 5 links). Names only - never the
    exception message, which can carry tokens, paths or payload data."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(names) < 5:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)
