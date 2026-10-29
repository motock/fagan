"""Cross-plan awareness: in-flight file overlap, ``blocked_by`` gaps and key conflicts.

Read-only helpers over the plan manifests. They never write, and every
unreadable or ambiguous input resolves toward "do not release work".
"""

import json
import logging
import threading
from pathlib import Path

__all__ = [
    "ACTIVE_STATUSES",
    "blocked_by_gaps",
    "clear_manifest_cache",
    "file_overlaps",
    "in_flight_stories",
    "key_conflicts",
    "load_manifests",
    "prune_manifest_cache",
    "read_manifest_cached",
]

logger = logging.getLogger("pipeline")

# Parsed manifests keyed by resolved path, stamped with (st_mtime_ns, st_size)
# so an unchanged file is never re-read or re-parsed. A long-lived process
# (the scheduler daemon scans about once a second) re-reads every neighbour
# manifest on every plan advance otherwise.
_MANIFEST_CACHE: dict[Path, tuple[int, int, dict]] = {}
# How the last read of a path failed: (st_mtime_ns, st_size, kind, error) where
# kind is "unreadable" or "not-object" and error is the JSON/OS error class
# name. Failures are never cached as values; this only lets load_manifests log
# each distinct failure stamp once instead of on every call.
_MANIFEST_FAILURES: dict[Path, tuple[int | None, int | None, str, str | None]] = {}
# The failure record already warned about, per path.
_MANIFEST_WARNED: dict[Path, tuple[int | None, int | None, str, str | None]] = {}
# Guards all three dicts above.
_MANIFEST_CACHE_LOCK = threading.Lock()


def clear_manifest_cache() -> None:
    """Forget every cached manifest, failure note and warning stamp."""
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE.clear()
        _MANIFEST_FAILURES.clear()
        _MANIFEST_WARNED.clear()


def read_manifest_cached(path: Path) -> dict | None:
    """Return the parsed manifest at *path*, reusing the cache when unchanged.

    The file is stat'ed on every call and the module-level cache is keyed on
    the resolved path with ``(st_mtime_ns, st_size, parsed)``; the file is
    re-read and re-parsed only when that stamp differs from the cached one, so
    a size change alone (a same-mtime rewrite on a coarse-granularity
    filesystem) is still picked up.

    Returns ``None`` when the file is absent, cannot be read, or does not
    parse to a JSON object. Such failures are never cached, so the next call
    retries them; an absent file also drops any stale cache entry.

    **The returned dict is shared, not a copy — callers must not mutate it.**
    Every caller only reads (``in_flight_stories``, ``file_overlaps``,
    ``blocked_by_gaps``, ``key_conflicts``, ``scan_done_markers``,
    ``_manifest_status_counts``), which is what keeps the cache safe.
    """
    key = Path(path).resolve()
    with _MANIFEST_CACHE_LOCK:
        try:
            stat = key.stat()
        except OSError as exc:
            # Absent or unstattable: drop any stale entry and report absence.
            _MANIFEST_CACHE.pop(key, None)
            _MANIFEST_FAILURES[key] = (None, None, "unreadable", type(exc).__name__)
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        entry = _MANIFEST_CACHE.get(key)
        if entry is not None and entry[0] == stamp[0] and entry[1] == stamp[1]:
            return entry[2]
        try:
            data = json.loads(key.read_text())
        except (OSError, ValueError) as exc:
            _MANIFEST_CACHE.pop(key, None)
            _MANIFEST_FAILURES[key] = (stamp[0], stamp[1], "unreadable", type(exc).__name__)
            return None
        if not isinstance(data, dict):
            _MANIFEST_CACHE.pop(key, None)
            _MANIFEST_FAILURES[key] = (stamp[0], stamp[1], "not-object", None)
            return None
        _MANIFEST_CACHE[key] = (stamp[0], stamp[1], data)
        _MANIFEST_FAILURES.pop(key, None)
        return data


def _warn_skipped_manifest(path: Path, plan: str) -> None:
    """Log why *path* produced no manifest, once per failure stamp.

    Repeats of the same failed stamp stay silent so one malformed file cannot
    flood the log; a fixed or rewritten file produces a new stamp and the
    next failure warns again.
    """
    key = Path(path).resolve()
    with _MANIFEST_CACHE_LOCK:
        failure = _MANIFEST_FAILURES.get(key)
        if failure is None or _MANIFEST_WARNED.get(key) == failure:
            return
        _MANIFEST_WARNED[key] = failure
    _, _, kind, error = failure
    if kind == "not-object":
        logger.warning("Skipping manifest that is not a JSON object", extra={"plan": plan})
    else:
        logger.warning(
            "Skipping unreadable manifest", extra={"plan": plan, "error": error}
        )

# States in which a story may still push commits to its branch.
ACTIVE_STATUSES = frozenset(
    {"in_progress", "interrupted", "changes_requested", "tests_passed", "pr_open"}
)
_SATISFIED_STATUSES = frozenset({"done", "merged"})


def load_manifests(plan_dir: Path, exclude_plan: str | None = None) -> dict[str, dict]:
    """Return ``{plan_name: manifest}`` for every readable manifest in *plan_dir*.

    Each file is read through :func:`read_manifest_cached`, so an unchanged
    manifest is parsed once per process rather than once per call. A malformed
    manifest is skipped with a warning so one bad file cannot blind the whole
    sweep; the warning is logged once per failure stamp, not on every call.
    Only manifests that exist now are returned.
    """
    directory = Path(plan_dir)
    paths = sorted(directory.glob("*.manifest.json"))
    # Forget entries for files that vanished: the glob can no longer reach
    # them, so nothing else would ever evict them.
    prune_manifest_cache(directory, paths)
    manifests: dict[str, dict] = {}
    for path in paths:
        plan = path.name.removesuffix(".manifest.json")
        if plan == exclude_plan:
            continue
        data = read_manifest_cached(path)
        if data is None:
            _warn_skipped_manifest(path, plan)
            continue
        manifests[plan] = data
    return manifests


def prune_manifest_cache(plan_dir: Path, paths: list[Path]) -> None:
    """Drop cache, failure and warning entries for manifests no longer present.

    *paths* is the directory's current glob result. Nothing else can reach an
    entry whose file vanished (the glob no longer yields it, so
    :func:`read_manifest_cached` is never called on it again), so a long-lived
    process would otherwise accumulate one parsed manifest per deleted plan.
    """
    directory = Path(plan_dir).resolve()
    present = {Path(path).resolve() for path in paths}
    with _MANIFEST_CACHE_LOCK:
        for key in [key for key in _MANIFEST_CACHE if key.parent == directory]:
            if key not in present:
                del _MANIFEST_CACHE[key]
        for key in [key for key in _MANIFEST_FAILURES if key.parent == directory]:
            if key not in present:
                del _MANIFEST_FAILURES[key]
                _MANIFEST_WARNED.pop(key, None)
        for key in [key for key in _MANIFEST_WARNED if key.parent == directory]:
            if key not in present:
                del _MANIFEST_WARNED[key]


def in_flight_stories(manifests: dict[str, dict]) -> list[dict]:
    """List ``{"plan", "key", "files"}`` for every story in an active status."""
    result = []
    for plan, manifest in manifests.items():
        for key, story in (manifest.get("stories") or {}).items():
            if story.get("status") not in ACTIVE_STATUSES:
                continue
            files = story.get("files")
            files = [f for f in files if isinstance(f, str)] if isinstance(files, list) else []
            result.append({"plan": plan, "key": key, "files": files})
    return result


def file_overlaps(files, others: list[dict]) -> list[dict]:
    """Return the entries of *others* that share at least one path with *files*."""
    if not files:
        return []
    wanted = set(files)
    return [other for other in others if wanted.intersection(other["files"])]


def blocked_by_gaps(story: dict, manifests: dict[str, dict]) -> list[str]:
    """Return the ``blocked_by`` references that are not yet satisfied.

    A reference is ``"<plan>:<story_key>"`` and is satisfied only when the
    target exists and is done or merged. Malformed or unknown references are
    returned as gaps: a typo must not release work meant to wait.
    """
    gaps = []
    for ref in story.get("blocked_by") or []:
        plan, sep, key = ref.partition(":") if isinstance(ref, str) else ("", "", "")
        target = ((manifests.get(plan) or {}).get("stories") or {}).get(key) if sep else None
        if not isinstance(target, dict) or target.get("status") not in _SATISFIED_STATUSES:
            gaps.append(ref)
    return gaps


def key_conflicts(keys, manifests: dict[str, dict], worktree_root) -> list[dict]:
    """Return the other-plan stories whose key collides with one of *keys*.

    A story's branch and worktree are derived from its key alone, so another
    plan's story with the same key (case-insensitively: branches are lowercased
    and macOS filesystems are case-insensitive) shares them. It conflicts unless
    it is done or merged and its worktree directory is gone. A missing status
    counts as unsatisfied: an unknown state must not release a collision.
    """
    wanted = {}
    for key in keys:
        if isinstance(key, str) and key:
            wanted.setdefault(key.lower(), []).append(key)
    conflicts = []
    for plan, manifest in manifests.items():
        stories = manifest.get("stories") if isinstance(manifest, dict) else None
        if not isinstance(stories, dict):
            continue
        for other_key, story in stories.items():
            if not isinstance(story, dict) or not isinstance(other_key, str):
                continue
            requested = wanted.get(other_key.lower())
            if not requested:
                continue
            status = story.get("status")
            if status in _SATISFIED_STATUSES and not (Path(worktree_root) / other_key).exists():
                continue
            for key in requested:
                conflicts.append(
                    {"key": key, "plan": plan, "other_key": other_key, "status": status}
                )
    return sorted(conflicts, key=lambda c: (c["plan"], c["other_key"]))
