"""Story-scoped commits of one story's record into a plan manifest.

WHY this module exists: a caller that reads a manifest, then spends minutes on
a slow phase (``review_story`` runs the reviewer model with the plan lock
released), cannot persist by writing the snapshot it read before that phase.
Any change another actor made in the window - an ``agent_done`` wake, an
operator ``set_story_status`` on a DIFFERENT story of the same plan - would be
silently overwritten. ``commit_story`` re-reads the manifest under the plan
lock and replaces only the named story, so concurrent updates to other stories
(and plan-level fields) survive.

The write goes through ``pipeline.server._atomic_write_json`` and the caller's
``manifest_path`` on purpose: both are the surfaces existing review tests patch
and observe.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_LOGGER = logging.getLogger("pipeline")

#: Retry cadence while another holder has the plan flock (seconds).
_ACQUIRE_RETRY_SECONDS = 0.1


def _write_story(
    plan_name: str, story_key: str, story: dict, manifest_path: Path
) -> None:
    """Replace one story in the freshly-read manifest and persist it."""
    # Module attributes are resolved at call time, never bound at import: the
    # package has an import cycle, and tests patch these attributes.
    from . import server

    manifest = json.loads(manifest_path.read_text())
    stories = manifest.get("stories")
    if not isinstance(stories, dict) or story_key not in stories:
        # A re-ingest dropped the story while the caller was working. Writing
        # it back would resurrect a record the manifest no longer has, so drop
        # the update and say so.
        _LOGGER.warning(
            "commit_story: story absent from the manifest, update not written",
            extra={"plan": plan_name, "story": story_key},
        )
        return
    stories[story_key] = story
    manifest["stories"] = stories
    server._atomic_write_json(manifest_path, manifest)


def commit_story(
    plan_name: str, story_key: str, story: dict, manifest_path: Path
) -> None:
    """Persist *story* as ``manifest["stories"][story_key]`` under the plan lock.

    Only that one key is replaced: other stories and every plan-level field are
    carried over from the manifest as it is on disk at commit time. Raises
    ``concurrency.PlanLockReacquireTimeout`` when the plan lock cannot be
    acquired within ``concurrency._plan_reacquire_timeout()``; nothing is
    written in that case.
    """
    from . import concurrency

    # Reentrancy: the plan lock is thread-reentrant via _held_plan_locks, so a
    # caller that already holds it (an advance_pipeline tick) must run the body
    # under that existing hold instead of re-flocking - a second exclusive
    # flock on a new fd would be refused even in this same thread.
    if plan_name in concurrency._held_plan_locks():
        _write_story(plan_name, story_key, story, manifest_path)
        return

    # Measure elapsed time with monotonic(): a backwards wall-clock step would
    # leave a time()-based deadline permanently in the future.
    deadline = time.monotonic() + concurrency._plan_reacquire_timeout()
    while True:
        with concurrency._plan_lock(plan_name) as acquired:
            if acquired:
                _write_story(plan_name, story_key, story, manifest_path)
                return
        if time.monotonic() >= deadline:
            raise concurrency.PlanLockReacquireTimeout(
                f"commit_story could not acquire the plan lock for plan "
                f"'{plan_name}' story '{story_key}' within "
                f"{concurrency._plan_reacquire_timeout():g}s"
            )
        time.sleep(_ACQUIRE_RETRY_SECONDS)
