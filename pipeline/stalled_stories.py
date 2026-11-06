"""Read-only listing of stalled, dispatch-eligible stories (liveness S7).

``pipeline.stall_scan.run_stall_scan`` answers "which stories are stalled?" but
only as a side effect of notifying: it emits one ``story_stalled`` message per
story per threshold window and records a high-water mark in
``_STALL_LAST_EMIT``. Scoped health (S7) needs to LIST the stalled stories on
every request without ever notifying, so it needs the same eligibility rule
without any of the announce-once machinery.

``stalled_stories`` therefore mirrors the scan's selection predicate and
returns plain data -- ``{"story", "status", "age_s"}`` per stalled story,
sorted by story key -- and nothing else: no summary, no path, no agent text.

ELIGIBILITY RULE: mirrored from ``stall_scan._run_stall_scan`` and must change
with it. A story is stalled when its status is one of
``stall_scan._STALL_STATUSES``, every dependency is in
``stall_scan._completed_dep_ids(stories)``, and
``stall_scan._stall_age_seconds(...)`` is not ``None`` and is strictly greater
than the caller's threshold. The shared helpers are imported rather than
copied, so the rule cannot drift silently.

PURE AND FAIL-OPEN: this module performs no I/O of its own, never notifies,
never touches ``_STALL_LAST_EMIT``, never calls the backend and never writes.
A malformed manifest (non-dict manifest, non-dict ``stories``, non-dict story)
yields an empty list or skips that story -- like the scan, it never raises.
"""

from __future__ import annotations

from .stall_scan import _STALL_STATUSES, _completed_dep_ids, _stall_age_seconds


def _story_dicts(stories: dict) -> dict[str, dict]:
    """Only the dict-valued stories: ``_completed_dep_ids`` assumes dicts."""
    return {key: story for key, story in stories.items() if isinstance(story, dict)}


def stalled_stories(
    plan_name: str, manifest: object, now: float, threshold: float
) -> list[dict]:
    """Read-only: the stalled stories of ``manifest`` as of ``now``.

    Returns ``[{"story": key, "status": status, "age_s": int}, ...]`` sorted by
    story key. ``threshold`` is in seconds and the comparison is strict, so a
    story whose age is exactly the threshold is NOT stalled.
    """
    if not isinstance(manifest, dict):
        return []
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        return []

    story_dicts = _story_dicts(stories)
    try:
        completed = _completed_dep_ids(story_dicts)
    except (AttributeError, KeyError, TypeError):
        # A dict story still missing the fields the shared helper reads is
        # malformed: claim no completions rather than raising or pretending.
        completed = set()

    stalled: list[dict] = []
    for story_key in sorted(story_dicts):
        story = story_dicts[story_key]
        status = story.get("status")
        if status not in _STALL_STATUSES:
            continue
        dependencies = story.get("dependencies") or []
        if not isinstance(dependencies, list):
            continue
        if not all(dep in completed for dep in dependencies):
            continue
        age = _stall_age_seconds(plan_name, story_key, story, now)
        if age is None or not age > threshold:
            continue
        stalled.append({"story": story_key, "status": status, "age_s": int(age)})
    return stalled
