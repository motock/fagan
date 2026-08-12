"""
Event guard utilities for the pipeline.

This module contains pure functions that are used by event handlers to
ensure they only mutate a story when it is in an expected state and, for
handlers that depend on commit SHAs, when the SHA has changed.

The implementation follows the tests in ``tests/unit/test_event_guards.py`` and respects the design rules: it has no side‑effects – all functions are pure.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# HANDLER_PRECONDITIONS mapping
# ---------------------------------------------------------------------------

HANDLER_PRECONDITIONS = {
    "story_ready": frozenset({"todo", "interrupted", "changes_requested"}),
    "agent_done": frozenset({"in_progress"}),
    "tests_passed": frozenset({"tests_passed"}),
    "ci_complete": frozenset({"pr_open"}),
    "story_done": frozenset({"done"}),
}

# ---------------------------------------------------------------------------
# precondition_met
# ---------------------------------------------------------------------------

def precondition_met(story: dict, event_type: str) -> bool:
    """Return ``True`` if the story's status is acceptable for *event_type*.

    The function is pure and never raises.  If *event_type* is unknown or
    the story has no ``status`` key, ``False`` is returned.
    """
    accepted = HANDLER_PRECONDITIONS.get(event_type)
    if not accepted:
        return False
    status = story.get("status")
    return status in accepted

# ---------------------------------------------------------------------------
# check_precondition
# ---------------------------------------------------------------------------

def check_precondition(manifest: dict, story_key: str, event_type: str) -> dict:
    """Guard that a handler may act on *story_key* for *event_type*.

    Parameters
    ----------
    manifest:
        The full pipeline manifest dictionary.  It must contain a ``stories``
        mapping; if missing, the story is considered unknown.
    story_key:
        Key of the story to check.
    event_type:
        Event type string.

    Returns
    -------
    dict
        * ``{"ok": True, "story": <story>}`` when the precondition holds.
        The returned ``story`` is the same object from the manifest (no copy).
        * ``{"ok": False, "skipped": "unknown_story"}`` if the story key does
          not exist in the manifest or the ``stories`` mapping itself is missing.
        * ``{"ok": False, "skipped": "precondition_not_met", "expected": [...],
           "actual": <status>}`` when the status does not match the accepted set.
          ``expected`` is a sorted list of strings; it may be empty if the
          event type is unknown.
    """
    # Defensive: treat missing 'stories' as unknown_story to avoid KeyError.
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        return {"ok": False, "skipped": "unknown_story"}

    story = stories.get(story_key)
    if story is None:
        return {"ok": False, "skipped": "unknown_story"}

    # Use precondition_met to decide.
    if not precondition_met(story, event_type):
        accepted = HANDLER_PRECONDITIONS.get(event_type, frozenset())
        expected_list = sorted(accepted)
        actual_status = story.get("status")
        return {
            "ok": False,
            "skipped": "precondition_not_met",
            "expected": expected_list,
            "actual": actual_status,
        }
    # All good.
    return {"ok": True, "story": story}

# ---------------------------------------------------------------------------
# sha_guard
# ---------------------------------------------------------------------------

def sha_guard(story: dict, head_sha: str | None, field: str) -> bool:
    """Return ``True`` if the handler should proceed based on SHA comparison.

    The guard is pure and follows these rules:
    * If ``head_sha`` is falsy (empty string or ``None``), return ``False``.
    * If the value stored in ``story[field]`` differs from ``head_sha``,
      return ``True``.
    * Otherwise, return ``False``.

    This mirrors the behaviour of the existing ``last_reviewed_sha`` idiom.
    """
    if not head_sha:
        # Covers both None and empty string.
        return False
    recorded = story.get(field)
    return head_sha != recorded

# End of module
