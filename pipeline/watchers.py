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
import time
from datetime import UTC, datetime

from . import audit_anchor, cross_plan, paths, security_audit
from .events import make_event
from .persistence import _notify_user
from .telemetry import CAPACITY_IN_USE_STATUS, update_manifest_snapshot

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

_SECURITY_DUE_COOLDOWN_SECONDS = 24 * 3600
_SECURITY_DUE_LAST_EMIT: dict[str, float] = {}


def _scan_security_audits(repo_plans: dict[str, str]) -> None:
    every_commits, every_days = security_audit.audit_thresholds(os.environ)
    if every_commits == 0 and every_days == 0:
        return
    for repo_root, plan_name in repo_plans.items():
        state = security_audit.read_audit_state(paths.PLAN_DIR, repo_root)
        commits = (
            security_audit.commits_since(repo_root, state["last_audited_sha"])
            if state
            else None
        )
        reason = security_audit.audit_due_reason(
            state, commits, datetime.now(UTC), every_commits, every_days
        )
        if reason is None:
            continue
        last_sha = state["last_audited_sha"] if state else None
        dedup_key = f"security_audit_due:{repo_root}:{last_sha or 'none'}"
        last_emit = _SECURITY_DUE_LAST_EMIT.get(dedup_key)
        now_mono = time.monotonic()
        if last_emit is not None and now_mono - last_emit < _SECURITY_DUE_COOLDOWN_SECONDS:
            continue
        _notify_user(
            plan_name,
            f"security audit due for {repo_root}: {reason}. Review it, then call record_security_audit to reset.",
            event="security_audit_due",
            dedup_key=dedup_key,
        )
        _SECURITY_DUE_LAST_EMIT[dedup_key] = now_mono


def _manifest_status_counts(manifest: dict) -> dict[str, int]:
    """Count one loaded manifest's stories by status (pure).

    Used by the scan tick to publish the gauge snapshot: the counts come from
    the manifest the scan already parsed, so collection never touches disk.
    """
    counts: dict[str, int] = {}
    stories = manifest.get("stories") or {}
    if not isinstance(stories, dict):
        return counts
    for story in stories.values():
        if not isinstance(story, dict):
            continue
        status = story.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    return counts


def scan_all_plans(bus):
    """Scan all plan manifests in :data:`PLAN_DIR` and publish events.

    The function iterates over every ``*.manifest.json`` file in the directory
    specified by :data:`pipeline.paths.PLAN_DIR`.  For each manifest it:

    * Reads it through :func:`pipeline.cross_plan.read_manifest_cached`, so a
      manifest whose file stamp is unchanged is not re-read or re-parsed on
      every tick; a missing or malformed file is skipped.

    * Skips any manifest that has a truthy ``paused`` key.
    * Delegates to :func:`scan_done_markers` for the actual event publishing.

    The returned list contains all events produced across every plan, flattened
    into a single list.
    """
    events: list[dict] = []
    repo_plans: dict[str, str] = {}
    status_counts: dict[str, int] = {}
    capacity_in_use = 0
    for path in sorted(paths.PLAN_DIR.glob("*.manifest.json")):
        plan_name = path.name.removesuffix(".manifest.json")
        manifest = cross_plan.read_manifest_cached(path)
        if manifest is None:
            logger.warning(
                "Skipping unreadable manifest for plan %s", plan_name, extra={"plan": plan_name}
            )
            continue
        if manifest.get("paused"):
            continue
        counts = _manifest_status_counts(manifest)
        capacity_in_use += counts.get(CAPACITY_IN_USE_STATUS, 0)
        for status, count in counts.items():
            status_counts[status] = status_counts.get(status, 0) + count
        events.extend(scan_done_markers(manifest, plan_name, bus))
        repo_root = manifest.get("repo_root")
        if repo_root and os.path.isdir(repo_root):
            repo_plans.setdefault(repo_root, plan_name)
    # Advisory check must never break the sweep or change its return value.
    try:
        _scan_security_audits(repo_plans)
    except Exception:
        logger.exception("security audit due check failed")
    # Ship each chain's head to the external anchor. A broken sink is a
    # WARNING, never a reason to skip the sweep: tamper evidence must not be
    # able to stop dispatch, and the scan's return value is unchanged either way.
    try:
        audit_anchor.anchor_heads()
    except Exception as exc:  # noqa: BLE001 - the sweep must survive a broken sink
        logger.warning("audit anchor pass failed: %s", type(exc).__name__)
    # Last statement of a successful tick: publish the gauge snapshot built
    # from the manifests above. A tick that raised never reaches this line, so
    # the previous complete snapshot stays published instead of being zeroed.
    update_manifest_snapshot(status_counts, capacity_in_use)
    return events