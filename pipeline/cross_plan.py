"""Cross-plan awareness: in-flight file overlap, ``blocked_by`` gaps and key conflicts.

Read-only helpers over the plan manifests. They never write, and every
unreadable or ambiguous input resolves toward "do not release work".
"""

import json
import logging
from pathlib import Path

__all__ = [
    "ACTIVE_STATUSES",
    "blocked_by_gaps",
    "file_overlaps",
    "in_flight_stories",
    "key_conflicts",
    "load_manifests",
]

# States in which a story may still push commits to its branch.
ACTIVE_STATUSES = frozenset(
    {"in_progress", "interrupted", "changes_requested", "tests_passed", "pr_open"}
)
_SATISFIED_STATUSES = frozenset({"done", "merged"})


def load_manifests(plan_dir: Path, exclude_plan: str | None = None) -> dict[str, dict]:
    """Return ``{plan_name: manifest}`` for every readable manifest in *plan_dir*.

    A malformed manifest is skipped with a warning so one bad file cannot
    blind the whole sweep.
    """
    manifests: dict[str, dict] = {}
    for path in sorted(Path(plan_dir).glob("*.manifest.json")):
        plan = path.name.removesuffix(".manifest.json")
        if plan == exclude_plan:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logging.getLogger("pipeline").warning(
                "Skipping unreadable manifest", extra={"plan": plan, "error": type(exc).__name__}
            )
            continue
        if not isinstance(data, dict):
            logging.getLogger("pipeline").warning(
                "Skipping manifest that is not a JSON object", extra={"plan": plan}
            )
            continue
        manifests[plan] = data
    return manifests


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
