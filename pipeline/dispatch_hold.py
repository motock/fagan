"""The deployment-wide dispatch hold and the running-agents read.

The ``dispatch_hold`` lock is a deployment-layer bool (see
``pipeline.settings_registry``). While it is True the scheduler dispatches no
new story and redispatches no interrupted or changes_requested story on any
plan. Review, CI polling and merges of stories already past dispatch continue,
and nothing in flight is interrupted by the hold itself.

Fail closed on the read. A store that offers no settings capability cannot have
a hold set, so it is not held. A store that offers the read but raises, or
holds a value that is not a bool, is held, with one WARNING.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pipeline.concurrency import _has_live_test_author_phase
from pipeline.settings_store import deployment_wide_namespaces, read_deployment_setting

logger = logging.getLogger("pipeline.dispatch_hold")

HOLD_KEY = "dispatch_hold"
#: The reason the scheduler reports when the hold stops dispatch.
HOLD_REASON = "dispatch_hold"


def dispatch_held(store: Any) -> bool:
    """True when the deployment layer holds dispatch, or the hold cannot be read."""
    try:
        records = store.records() if callable(getattr(store, "records", None)) else None
        if getattr(records, "get_settings", None) is None:
            return False
        value = read_deployment_setting(records, HOLD_KEY, False)
    except Exception as exc:  # noqa: BLE001 - a hold that cannot be read is a held hold
        logger.warning(
            "dispatch hold unreadable; treating dispatch as held",
            extra={"error_type": type(exc).__name__},
        )
        return True
    if not isinstance(value, bool):
        logger.warning("dispatch hold is not a bool; treating dispatch as held")
        return True
    return value


def filter_ready(ready: list[str], held: bool) -> list[str]:
    """The ready stories the tick may dispatch: none while the hold is on."""
    return [] if held else ready


def pause_reason(held: bool) -> dict[str, str]:
    """The result field naming the hold as the reason dispatch is paused, if it is."""
    return {"dispatch_pause_reason": HOLD_REASON} if held else {}


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_running(story: dict) -> bool:
    if _has_live_test_author_phase(story):
        return True
    return story.get("status") == "in_progress" and _pid_alive(story.get("pid"))


def running_agents(store: Any) -> dict[str, Any]:
    """The stories whose agent process is running, across every namespace the store lists.

    A store without namespace listing is read for the default namespace only,
    and ``scope`` says which was read.
    """
    records = store.records()
    scope, namespaces = deployment_wide_namespaces(records)
    stories = []
    for namespace in namespaces:
        for plan, manifest in records.get_manifests(namespace).items():
            for key, story in manifest.get("stories", {}).items():
                if _is_running(story):
                    stories.append({"plan": plan, "story": key})
    return {"scope": scope, "stories": stories}
