"""Fences that keep an abandoned dispatch from spawning a second agent.

Defect (b3+b4 runs on the standalone stack, 2026-11-09/10): the scheduler's
reconcile worker is abandoned when it passes the 900s join deadline
(``scheduler_daemon._run_with_watchdog``). A Python thread cannot be killed,
so the abandoned worker kept running its dispatch - after the baseline,
inside the slow prompt-build/model phase, before the agent spawn. The
dispatch lease (``pipeline/dispatch_lease.py``) is claimed by the daemon's
pid and ``lease_is_live`` treats a dead owner as NOT live, so killing the
daemon released the lease while its in-flight dispatch was still alive: the
fresh daemon reclaimed the story and two agents shared one worktree.

Three layers, all fail-safe (an unreadable worktree never raises and never
withholds work):

1. ``dispatch_side_effect_in_progress`` - the worktree exists and shows work
   in progress (the baseline marker, or commits beyond the base branch).
2. ``blocks_reclaim`` - the lease exists and is not live (owner pid gone or
   expired) while a side effect is in progress, so a reclaim is withheld.
3. ``mark_worker_abandoned`` / ``worker_is_abandoned`` - an in-process
   registry of abandoned worker threads, consulted by ``executor_fence``
   immediately before the agent spawn. This turns "the worker cannot be
   killed" into "the worker cannot launch an agent": the abandoned worker
   itself is fenced even though it still owns its lease.

``executor_fence`` is the pre-spawn gate wired into ``pipeline/dispatch.py``
after every slow phase. It refuses ONLY on positive evidence - an abandoned
worker, a terminal story, or a lease still held by another live process -
because ``dispatch_story`` is also called with no scheduler lease at all (the
MCP tool, startup reconcile, direct callers) and missing evidence must never
withhold work. ``lease_is_live``'s documented contract is deliberately
unchanged: the side-effect rule belongs to this caller, not to the pure lease
predicate.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from .dispatch_lease import lease_is_live

#: Marker written by the baseline test phase.
BASELINE_MARKER = ".dispatch_baseline_test_checked"

#: Statuses after which a spawn is positively unwanted.
TERMINAL_STATUSES = frozenset({"done", "merged"})

_LOG = logging.getLogger(__name__)

#: Base refs tried, in order, when counting the branch's own commits.
_BASE_REFS = ("origin/master", "master", "origin/main", "main")

_GIT_TIMEOUT_S = 15

# Abandoned worker threads, keyed by ``id(thread)``. The thread object is
# kept as the value so its id cannot be recycled while the entry lives.
_ABANDONED_WORKERS: dict[int, object] = {}
_ABANDONED_LOCK = threading.Lock()


def _prune_dead_workers_locked() -> None:
    """Drop entries whose thread has finished (caller holds the lock)."""
    for key, thread in list(_ABANDONED_WORKERS.items()):
        is_alive = getattr(thread, "is_alive", None)
        if callable(is_alive) and not is_alive():
            _ABANDONED_WORKERS.pop(key, None)


def mark_worker_abandoned(thread) -> None:
    """Record ``thread`` as abandoned: it may no longer launch an agent.

    Never raises; the registry only ever answers for thread identities.
    """
    try:
        with _ABANDONED_LOCK:
            _prune_dead_workers_locked()
            _ABANDONED_WORKERS[id(thread)] = thread
    except Exception:  # noqa: BLE001 - pragma: no cover - never raise
        return


def worker_is_abandoned(thread) -> bool:
    """True iff ``thread`` was marked abandoned and is still running."""
    try:
        with _ABANDONED_LOCK:
            _prune_dead_workers_locked()
            return id(thread) in _ABANDONED_WORKERS
    except Exception:  # noqa: BLE001 - pragma: no cover - never raise
        return False


def _has_commits_beyond_base(path: Path) -> bool:
    """True when ``path``'s branch carries commits its base ref lacks.

    Every failure (no git, unreadable repo, no resolvable base ref,
    unparseable output) reads as "no commits": the fence must never
    withhold work on evidence it could not verify.
    """
    for base in _BASE_REFS:
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "rev-list", "--count", f"{base}..HEAD"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        if proc.returncode != 0:
            continue
        try:
            return int(proc.stdout.strip()) > 0
        except (TypeError, ValueError):
            return False
    return False


def dispatch_side_effect_in_progress(worktree) -> bool:
    """True when ``worktree`` shows a dispatch's side effects.

    A side effect is in progress when the worktree directory exists AND
    either the baseline marker exists or the branch has commits beyond its
    base. An absent, unreadable, or malformed ``worktree`` reads as False
    (never withhold work) and never raises.
    """
    try:
        if not worktree:
            return False
        path = Path(os.fspath(worktree))
        if not path.is_dir():
            return False
        if (path / BASELINE_MARKER).exists():
            return True
        return _has_commits_beyond_base(path)
    except Exception:  # noqa: BLE001 - fail safe, never raise
        return False


def blocks_reclaim(story, worktree) -> bool:
    """True when a lease reclaim must be withheld for ``story``.

    Withheld iff the story carries a lease that is NOT live (by owner pid
    or expiry - see ``lease_is_live``) AND its worktree shows a side effect
    in progress: the previous dispatch's scheduler is gone but its work
    survived it, so dispatching now would put a second agent on one
    worktree. No lease, a live lease, or no side effect all read False.
    Never raises.
    """
    try:
        if not isinstance(story, dict):
            return False
        if not story.get("dispatch_lease_expires_at"):
            return False
        if lease_is_live(story):
            return False
        return dispatch_side_effect_in_progress(worktree)
    except Exception:  # noqa: BLE001 - fail safe, never raise
        return False


def _read_story(store, plan_name: str, story_key: str) -> dict:
    """Re-read one story straight from the persisted manifest."""
    path = store.manifest_path(plan_name)
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    stories = manifest.get("stories") or {}
    return stories.get(story_key) or {}


def _lease_lost_error() -> dict:
    return {"ok": False, "error": "dispatch lease lost"}


def executor_fence(plan_name: str, story_key: str, store) -> dict | None:
    """The pre-spawn gate: ``None`` to spawn, an error dict to abort.

    Runs after every slow phase, immediately before the agent is spawned.
    It refuses ONLY on positive evidence:

    1. the in-process abandonment registry - an abandoned worker must not
       launch an agent even though it still owns its lease
       (``dispatch abandoned``);
    2. the story has already reached a terminal status (``done``/``merged``):
       a spawn now would be work nobody asked for;
    3. the story, re-read under the plan lock, carries a lease whose owner
       pid is set, differs from this process, and is still live - another
       process owns this dispatch (``dispatch lease lost``).

    Everything else proceeds. ``dispatch_story`` is also called with no
    scheduler lease at all (the MCP ``dispatch_story`` tool, startup
    reconcile, and direct callers), so absent lease fields, an
    unacquirable plan lock, an unreadable manifest, or any exception must
    NOT withhold work: this module's whole contract is to fail safe.
    Aborting never mutates the story: the caller returns the error dict
    untouched.
    """
    from .concurrency import _plan_lock

    if worker_is_abandoned(threading.current_thread()):
        return {"ok": False, "error": "dispatch abandoned"}
    try:
        with _plan_lock(plan_name) as acquired:
            if not acquired:
                return None
            story = _read_story(store, plan_name, story_key)
    except Exception:  # noqa: BLE001 - fail safe: never withhold work
        _LOG.warning(
            "dispatch fence: cannot re-read %s/%s; proceeding",
            plan_name,
            story_key,
            exc_info=True,
        )
        return None
    if not isinstance(story, dict):
        return None
    if story.get("status") in TERMINAL_STATUSES:
        return _lease_lost_error()
    owner = story.get("dispatch_lease_owner_pid")
    if not owner:
        return None
    try:
        owned_elsewhere = int(owner) != os.getpid()
    except (TypeError, ValueError):
        return None
    if not owned_elsewhere:
        return None
    if lease_is_live(story):
        return _lease_lost_error()
    return None
