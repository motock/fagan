"""
Watcher utilities for the event‑driven pipeline.

This module implements ``scan_done_markers`` which scans a manifest for
``.agent_done`` marker files written by dispatched agents and publishes an
``agent_done`` event on the supplied EventBus.  The function is intentionally
lightweight: it never runs tests, grades stories, mutates the manifest or
touches any external processes.
"""

import json
import logging
import os

from .events import make_event

logger = logging.getLogger(__name__)


def scan_done_markers(manifest: dict, plan: str, bus) -> list[dict]:
    """Scan ``manifest`` for agent completion markers.

    Parameters
    ----------
    manifest:
        The pipeline manifest dictionary.  It must contain a top‑level
        ``stories`` mapping of story keys to story dictionaries.
    plan:
        Identifier of the current plan; passed straight through to
        :func:`pipeline.events.make_event`.
    bus:
        An object with a ``publish(event: dict)`` method.  The tests use a
        lightweight fake bus that records published events.

    Returns
    -------
    list[dict]
        A list of the event dictionaries that were published during this scan.

    Notes
    -----
    * Only stories with ``status == 'in_progress'`` are considered.
    * Stories without a ``worktree`` key are ignored.
    * If the worktree directory does not exist on disk, the story is silently
      skipped.
    * A marker file named ``.agent_done`` in the worktree triggers an event.
      The file contents must be valid JSON; otherwise a warning is logged and
      the marker is left untouched for debugging.
    * After successfully publishing an event the marker file is renamed to
      ``.agent_done.consumed`` using :func:`os.replace` so it will not be
      processed again on subsequent scans.
    """

    events: list[dict] = []

    stories = manifest.get("stories", {}) or {}
    for story_key, story in stories.items():
        # Skip non‑in_progress stories.
        if story.get("status") != "in_progress":
            continue

        worktree = story.get("worktree")
        if not worktree:
            continue

        # Ensure the directory exists; skip silently otherwise.
        if not os.path.isdir(worktree):
            continue

        marker_path = os.path.join(worktree, ".agent_done")
        if not os.path.exists(marker_path):
            continue

        try:
            with open(marker_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError:
            logger.warning(
                "Malformed .agent_done marker in %s – skipping; file left for debugging",
                marker_path,
            )
            continue
        except Exception:  # pragma: no cover – defensive, unlikely.
            logger.exception("Unexpected error reading %s", marker_path)
            continue

        event = make_event(
            "agent_done", plan, story_key=story_key, payload=payload
        )
        bus.publish(event)
        events.append(event)

        # Rename the marker to indicate it has been consumed.
        try:
            os.replace(marker_path, os.path.join(worktree, ".agent_done.consumed"))
        except Exception:  # pragma: no cover – unlikely but safe.
            logger.exception("Failed to rename %s", marker_path)

    return events
