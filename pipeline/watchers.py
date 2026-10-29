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
import stat
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


#: Story-field paths that point at a detached job's result file, in lookup
#: order. Each entry is the path of keys inside one story dict, so
#: ``("merge_reverify", "result_path")`` means ``story["merge_reverify"]
#: ["result_path"]``. Cumulative: a new detached job appends its own path here.
#: The scan wakes a story as soon as *any* of these files exists.
JOB_RESULT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("grading_result_path",),
    ("merge_reverify", "result_path"),
)

#: Result stamps already announced to the bus this process:
#: ``(plan, story key, result path, st_mtime_ns)``. A rewritten result file has
#: a new ``st_mtime_ns`` and therefore wakes the plan again; re-reading the
#: same file on the next scan (about once a second) does not. Entries for
#: stories a manifest no longer lists are dropped, so the set stays bounded by
#: the number of live detached jobs.
_PUBLISHED_JOB_RESULTS: set[tuple[str, str, str, int]] = set()

#: Last observed ``{story key: status}`` map per plan, from the previous pass.
#: The manifest cache in :mod:`pipeline.cross_plan` holds only the latest parse,
#: so the transition *into* ``done`` can only be seen by keeping it here.
_PREVIOUS_STATUS: dict[str, dict[str, str]] = {}


def _stories(manifest: dict) -> dict:
    """Return the manifest's ``stories`` mapping, or an empty one.

    Defensive on purpose: this runs inside the scheduler's scan loop, where a
    malformed neighbour manifest must never stop the sweep.
    """
    if not isinstance(manifest, dict):
        return {}
    stories = manifest.get("stories")
    return stories if isinstance(stories, dict) else {}


def _field_value(story: object, field_path: tuple[str, ...]):
    """Return the value at *field_path* inside *story*, or ``None``.

    A missing key or a non-dict value part-way down the path (a hand-edited
    ``merge_reverify: "pending"``) yields ``None`` instead of raising.
    """
    value = story
    for name in field_path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def _status_map(manifest: dict) -> dict[str, str]:
    """Return ``{story key: status}`` for every well-formed story in *manifest*."""
    return {
        str(story_key): story["status"]
        for story_key, story in _stories(manifest).items()
        if isinstance(story, dict) and isinstance(story.get("status"), str)
    }


def job_results_ready(plan: str, manifest: dict) -> list[str]:
    """Return the story keys whose detached job has a brand-new result file.

    A story is ready when any path in :data:`JOB_RESULT_FIELDS` resolves to an
    existing regular file. Only the first sighting of a
    ``(plan, story, result path, st_mtime_ns)`` stamp is reported: the scan
    runs about once a second and must not re-wake the same finished job. A
    rewritten result file carries a new stamp and is reported again.

    This is a hint, not a decision: the caller publishes a ``reconcile`` event
    and the advance re-reads the manifest, including the result file, itself.
    """
    ready: list[str] = []
    listed: set[str] = set()
    for story_key, story in _stories(manifest).items():
        key = str(story_key)
        listed.add(key)
        for field_path in JOB_RESULT_FIELDS:
            result = _field_value(story, field_path)
            if not isinstance(result, str) or not result:
                continue
            try:
                info = os.stat(result)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            stamp = (plan, key, result, info.st_mtime_ns)
            if stamp in _PUBLISHED_JOB_RESULTS:
                continue
            _PUBLISHED_JOB_RESULTS.add(stamp)
            ready.append(key)
            # One wake per story even if two of its result files appear at
            # once: the advance re-reads the manifest and sees them both.
            break
    _PUBLISHED_JOB_RESULTS.difference_update(
        {
            stamp
            for stamp in _PUBLISHED_JOB_RESULTS
            if stamp[0] == plan and stamp[1] not in listed
        }
    )
    return ready


def newly_done(previous: dict[str, str] | None, current: dict) -> list[str]:
    """Return the story keys that just became ``done``, in manifest order.

    Parameters
    ----------
    previous:
        ``{story key: status}`` from the previous scan of the same plan. ``None``
        means this process has never scanned that plan (first sight, e.g. right
        after a daemon restart): nothing is newly done, so a restart does not
        wake every plan that depends on long-finished stories.
    current:
        The manifest as just read.
    """
    if previous is None:
        return []
    return [
        str(story_key)
        for story_key, story in _stories(current).items()
        if isinstance(story, dict)
        and story.get("status") == "done"
        and previous.get(str(story_key)) != "done"
    ]


def _blocked_by_wakes(
    manifests: dict[str, dict], transitions: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Map each dependent plan to the ``"<plan>:<story>"`` blockers it waits on.

    *manifests* is every manifest this pass read and *transitions* maps a plan
    to the story keys that just became ``done`` in it. A dependent plan is
    reported once however many of its blockers finished, so one pass wakes it
    once.
    """
    finished = {
        f"{plan}:{story_key}"
        for plan, story_keys in transitions.items()
        for story_key in story_keys
    }
    wakes: dict[str, list[str]] = {}
    for dependent_plan, manifest in manifests.items():
        for story in _stories(manifest).values():
            if not isinstance(story, dict):
                continue
            blocked_by = story.get("blocked_by")
            if not isinstance(blocked_by, list):
                continue
            hits = [
                blocker
                for blocker in blocked_by
                if isinstance(blocker, str) and blocker in finished
            ]
            if hits:
                wakes.setdefault(dependent_plan, []).extend(hits)
    return wakes


def scan_all_plans(bus):
    """Scan all plan manifests in :data:`PLAN_DIR` and publish events.

    The function iterates over every ``*.manifest.json`` file in the directory
    specified by :data:`pipeline.paths.PLAN_DIR`.  For each manifest it:

    * Reads it through :func:`pipeline.cross_plan.read_manifest_cached`, so a
      manifest whose file stamp is unchanged is not re-read or re-parsed on
      every tick; a missing or malformed file is skipped.

    * Skips any manifest that has a truthy ``paused`` key.
    * Delegates to :func:`scan_done_markers` for the ``agent_done`` publishing.
    * Wakes detached-job and blocked-plan work as a ``reconcile`` event:
      ``{"reason": "job_result", "stories": [...]}`` for a plan with a
      finished detached job (see :func:`job_results_ready`), and
      ``{"reason": "blocked_by_done"}`` for a plan whose ``blocked_by`` names
      a story that just became ``done`` (see :func:`newly_done`).

    A wake only enqueues the plan: the advance re-reads the manifest and
    decides everything from it. A wake pass that raises is logged at WARNING
    and changes neither the sweep nor this function's return value.

    The returned list contains all events produced across every plan, flattened
    into a single list.
    """
    events: list[dict] = []
    repo_plans: dict[str, str] = {}
    status_counts: dict[str, int] = {}
    capacity_in_use = 0
    # Every manifest this pass read, plus the story keys that just became done
    # in each: a dependent plan may sort before or after its blocker, so the
    # blocked_by resolution runs after the loop over these two maps.
    manifests: dict[str, dict] = {}
    transitions: dict[str, list[str]] = {}
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
        manifests[plan_name] = manifest
        counts = _manifest_status_counts(manifest)
        capacity_in_use += counts.get(CAPACITY_IN_USE_STATUS, 0)
        for status, count in counts.items():
            status_counts[status] = status_counts.get(status, 0) + count
        events.extend(scan_done_markers(manifest, plan_name, bus))
        # A finished detached job is visible in this manifest alone. The wake
        # is a hint: the advance re-reads the manifest and the result file.
        try:
            ready = job_results_ready(plan_name, manifest)
        except Exception as exc:  # noqa: BLE001 - one broken wake must not stop the sweep
            logger.warning(
                "job_result wake pass failed: %s",
                type(exc).__name__,
                extra={"plan": plan_name},
            )
        else:
            if ready:
                event = make_event(
                    "reconcile",
                    plan_name,
                    payload={"reason": "job_result", "stories": ready},
                )
                bus.publish(event)
                events.append(event)
        # Remember the statuses for the next pass; ``newly_done`` reports the
        # transition into ``done`` that only a previous pass can reveal.
        try:
            just_done = newly_done(_PREVIOUS_STATUS.get(plan_name), manifest)
        except Exception as exc:  # noqa: BLE001 - one broken wake must not stop the sweep
            logger.warning(
                "blocked_by wake pass failed: %s",
                type(exc).__name__,
                extra={"plan": plan_name},
            )
        else:
            if just_done:
                transitions[plan_name] = just_done
        # Remember every status for the next pass, whatever this one concluded:
        # the wake above only reports the transition, this map detects it.
        _PREVIOUS_STATUS[plan_name] = _status_map(manifest)
        repo_root = manifest.get("repo_root")
        if repo_root and os.path.isdir(repo_root):
            repo_plans.setdefault(repo_root, plan_name)
    if transitions:
        try:
            wakes = _blocked_by_wakes(manifests, transitions)
        except Exception as exc:  # noqa: BLE001 - one broken wake must not stop the sweep
            logger.warning("blocked_by resolution failed: %s", type(exc).__name__)
        else:
            for dependent_plan, blockers in wakes.items():
                event = make_event(
                    "reconcile", dependent_plan, payload={"reason": "blocked_by_done"}
                )
                bus.publish(event)
                events.append(event)
                logger.info(
                    "woke plan %s: blocking stories finished (%d)",
                    dependent_plan,
                    len(blockers),
                    extra={"plan": dependent_plan, "stories": len(blockers)},
                )
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