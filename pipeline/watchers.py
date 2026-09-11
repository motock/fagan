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

from . import paths
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
    * The marker file is renamed to ``.agent_done.consumed`` with
      :func:`os.replace` BEFORE the event is published.  The rename is the
      claim: two concurrent scan passes can be in flight at once (the
      scheduler watchdog abandons a slow worker thread without killing it),
      and only the pass that wins the rename ever publishes the marker, so a
      story can never be double‑dispatched.
    * ``FileNotFoundError`` from the rename means another pass already claimed
      the marker; that pass logs at DEBUG and publishes nothing.  Any other
      ``OSError`` is logged as an ERROR and the marker is left on disk
      unrenamed, so a later scan can claim and publish it once the problem is
      fixed.
    * If ``bus.publish`` raises after a successful claim, the marker is
      already consumed and that completion is NOT re‑published.  This is
      deliberate: ``advance_all_plans``' reconcile sweep independently reaps
      ``in_progress`` stories whose pid is dead, so a dropped marker costs one
      reconcile cycle, whereas a double‑publish costs a plan‑lock pile‑up.
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
            # Validate payload is a dict
            if not isinstance(payload, dict):
                logger.warning(
                    "Non-dict .agent_done marker in %s – skipping; file left for debugging",
                    marker_path,
                )
                continue
        except json.JSONDecodeError:
            logger.warning(
                "Malformed .agent_done marker in %s – skipping; file left for debugging",
                marker_path,
            )
            continue
        except Exception:
            logger.exception("Unexpected error reading %s", marker_path)
            continue

        # Claim the marker BEFORE publishing.  The bus is in-process and
        # synchronous and publish can run for minutes while holding the plan
        # lock, so renaming first is the only way to guarantee that exactly
        # one concurrent scan pass publishes a given marker.
        try:
            os.replace(marker_path, os.path.join(worktree, ".agent_done.consumed"))
        except FileNotFoundError:
            # Another scan pass already claimed this marker.  Expected and
            # benign when two passes race; log quietly and publish nothing.
            logger.debug(
                "Marker %s already claimed by another scan pass", marker_path
            )
            continue
        except OSError:
            # The marker could not be claimed; leave it on disk unrenamed so
            # a later scan can retry.  Never publish a marker we could not
            # claim.
            logger.exception("Failed to rename %s", marker_path)
            continue

        event = make_event("agent_done", plan, story_key=story_key, payload=payload)
        bus.publish(event)
        events.append(event)

    return events

def scan_all_plans(bus):
    """Scan all plan manifests in :data:`PLAN_DIR` and publish events.

    The function iterates over every ``*.manifest.json`` file in the directory
    specified by :data:`pipeline.paths.PLAN_DIR`.  For each manifest it:

    * Parses the JSON; if the file is missing or malformed a warning is logged
      and the plan is skipped.
    * Skips any manifest that has a truthy ``paused`` key.
    * Delegates to :func:`scan_done_markers` for the actual event publishing.

    The returned list contains all events produced across every plan, flattened
    into a single list.
    """
    events: list[dict] = []
    for path in sorted(paths.PLAN_DIR.glob("*.manifest.json")):
        plan_name = path.name.removesuffix(".manifest.json")
        try:
            manifest_text = path.read_text()
            manifest = json.loads(manifest_text)
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - exercised via tests
            logger.warning("Failed to read or parse %s: %s", path, exc)
            continue
        if manifest.get("paused"):
            continue
        events.extend(scan_done_markers(manifest, plan_name, bus))
    return events