"""Read tools that list a plan's stories and read one story's brief.

``list_plan_stories`` answers "which stories does this plan hold, and in what
state?" and ``get_story`` answers "what is this story's brief?", so a client
can find a parked story without already knowing its key. Both are plain reads
of the manifest in the namespace bound for the call: nothing is written, and
the manifest's own dicts are never mutated.

Only the fields listed in each tool's reply are returned. Every free-text
value goes through :func:`redact` first and is cut afterwards, so a secret
cannot be sliced in half and escape the masking patterns.
"""

from __future__ import annotations

from typing import Any

from .module_ref import _ModuleRef
from .story_evidence import NOT_FOUND, redact

_store = _ModuleRef("pipeline.server", "_store")
_service = _ModuleRef("pipeline.server", "_service")
_validate_key = _ModuleRef("pipeline.server", "_validate_key")

#: Most stories one ``list_plan_stories`` reply carries.
LIST_STORIES_MAX = 500
#: Longest summary or reason returned by ``list_plan_stories``.
LIST_TEXT_MAX = 300
#: Longest brief returned by ``get_story``.
BRIEF_MAX = 60000


def _masked(value: object) -> str | None:
    """Return *value* masked, or None when it is not a string."""
    return redact(value) if isinstance(value, str) else None


def _list_of(value: object) -> list[Any]:
    """Return a copy of *value* when it is a list, else an empty list."""
    return list(value) if isinstance(value, list) else []


def _stories_of(plan_name: str) -> dict[str, Any] | None:
    """Return the plan's stories dict from the bound namespace, or None."""
    manifest = _store.get_manifest_or_none(plan_name)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    return stories if isinstance(stories, dict) else None


def _story_item(key: str, story: dict[str, Any], ready: bool) -> dict[str, Any]:
    """Return one story's row for the listing."""
    return {
        "key": key,
        "summary": _cut(_masked(story.get("summary")), LIST_TEXT_MAX),
        "status": story.get("status"),
        "risk": story.get("risk"),
        "dependencies": _list_of(story.get("dependencies")),
        "blocked_by": _list_of(story.get("blocked_by")),
        "pr_url": story.get("pr_url"),
        "parked_reason": _cut(_masked(story.get("parked_reason")), LIST_TEXT_MAX),
        "failure_reason": _cut(_masked(story.get("failure_reason")), LIST_TEXT_MAX),
        "ready": ready,
    }


def _cut(text: str | None, limit: int) -> str | None:
    """Return *text* cut to *limit* characters, or None when there is none."""
    return None if text is None else text[:limit]


def list_plan_stories(plan_name: str) -> dict[str, Any]:
    """
    Return every story in the plan, in manifest order, with its state.

    plan_name: the plan's name, as returned by list_plans.

    Returns {"ok": true, "plan": ..., "stories": [...], "counts": {...}}. Each
    story is {"key", "summary", "status", "risk", "dependencies",
    "blocked_by", "pr_url", "parked_reason", "failure_reason", "ready"}; the
    summary and reasons are masked and cut to 300 characters. "ready" is true
    exactly for the stories list_ready_stories returns. "counts" tallies every
    story by status. At most 500 stories are listed; "truncated": true is added
    when the plan holds more. The brief and the runtime fields are not returned:
    use get_story for a brief.

    An unknown plan, or a manifest whose stories are not a dict, returns
    {"ok": false, "status": 404, "error": ...}.
    """
    _validate_key(plan_name)
    stories = _stories_of(plan_name)
    if stories is None:
        return dict(NOT_FOUND)
    ready = {entry["key"] for entry in _service.list_ready_stories(plan_name)}
    items = list(stories.items())
    counts: dict[str, int] = {}
    for _key, story in items:
        status = story.get("status") if isinstance(story, dict) else None
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    listed = [
        _story_item(key, story if isinstance(story, dict) else {}, key in ready)
        for key, story in items[:LIST_STORIES_MAX]
    ]
    result: dict[str, Any] = {"ok": True, "plan": plan_name, "stories": listed, "counts": counts}
    if len(items) > LIST_STORIES_MAX:
        result["truncated"] = True
    return result


def get_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Return one story's brief and its state, with secret values masked.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.

    Returns {"ok": true, "key", "summary", "status", "risk", "persona",
    "dependencies", "blocked_by", "files", "pr_url", "attempts",
    "parked_reason", "failure_reason", "agent_instructions"}. The brief is
    masked and cut to 60000 characters; "agent_instructions_truncated": true
    is added when it was cut. No other manifest field is returned.

    An unknown plan or story returns {"ok": false, "status": 404, "error": ...}.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    stories = _stories_of(plan_name)
    story = stories.get(story_key) if stories is not None else None
    if not isinstance(story, dict):
        return dict(NOT_FOUND)
    brief = _masked(story.get("agent_instructions"))
    result: dict[str, Any] = {
        "ok": True,
        "key": story_key,
        "summary": _masked(story.get("summary")),
        "status": story.get("status"),
        "risk": story.get("risk"),
        "persona": story.get("persona"),
        "dependencies": _list_of(story.get("dependencies")),
        "blocked_by": _list_of(story.get("blocked_by")),
        "files": _list_of(story.get("files")),
        "pr_url": story.get("pr_url"),
        "attempts": story.get("attempts"),
        "parked_reason": _masked(story.get("parked_reason")),
        "failure_reason": _masked(story.get("failure_reason")),
        "agent_instructions": _cut(brief, BRIEF_MAX),
    }
    if brief is not None and len(brief) > BRIEF_MAX:
        result["agent_instructions_truncated"] = True
    return result
