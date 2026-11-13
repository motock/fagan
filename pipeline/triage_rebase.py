"""Overlord executor for ``resolve_rebase`` (ov-3): park class ``rebase_conflict``.

The overlord never edits the repo. When a story's branch conflicts with the
base, this executor re-derives the conflict from git (``git merge-tree``),
checks its preconditions, and either:

* unparks the story when the conflict is gone (``clear_park_for_unpark``, the
  same step ``retry_transient`` uses), or
* hands the worker a task built deterministically from the observed paths,
  through the ``triage_diagnosis`` channel, and sets the story to ``todo``.

Every refusal parks the story for a human with the reason stated. The
``parked_reason`` text is never evidence: the conflicting paths come from git.
The task text is a pure function of the observed evidence, so identical
evidence always yields identical text.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime

from .module_ref import _ModuleRef
from .persistence import _notify_user
from .triage_park_classes import _is_test_path, check_ruling_allowed, classify_park
from .triage_rebrief import DIAGNOSIS_HISTORY, clear_park_for_unpark

_park = _ModuleRef("pipeline.triage", "_park")
_triage_mode = _ModuleRef("pipeline.triage", "_triage_mode")

#: Attempts a story may spend on this action, ever.
RESOLVE_REBASE_ATTEMPT_CAP = 3
#: Most commits a branch may be ahead of the base for a worker to rebase it.
RESOLVE_REBASE_MAX_COMMITS_AHEAD = 5
_GIT_TIMEOUT_SECONDS = 120

#: Paths a worker must never rebase through: workflows, lockfiles, migrations,
#: key material, and the overlord's own authority file. A conflict in any of
#: them parks the story for a human, whatever the story declares in ``files``.
DENIED_PATH_PATTERNS = (
    re.compile(r"^\.github/workflows/"),
    re.compile(r"\.lock$"),
    re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|go\.sum)$"),
    re.compile(r"(^|/)(migrations?|migrate)/"),
    re.compile(r"\.(pem|key|p12|pfx|jks|keystore|asc|gpg)$"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"(^|/)overlord-authority\.json$"),
)

_CONFLICT_INFO_SEPARATOR = "\t"


def is_denied_path(path: str) -> bool:
    """True when ``path`` falls in a protected category (see ``DENIED_PATH_PATTERNS``)."""
    lowered = path.lower()
    return any(pattern.search(lowered) for pattern in DENIED_PATH_PATTERNS)


def build_rebase_task(base: str, paths: list[str], branch: str) -> str:
    """The worker's task for a rebase conflict. Pure: same inputs, same text."""
    listed = ", ".join(sorted(paths))
    return (
        f"Your branch conflicts with {base} in: {listed}. "
        f"Rebase onto origin/{base}. "
        f"For a modify/delete conflict where {base} deleted the file, accept the deletion and move "
        f"your intent to its replacement named in {base}'s history if the file was replaced; "
        f"otherwise git rm it. "
        f"For a file whose entries are independent lines (a registry/allow-list dict or list "
        f"literal), take {base}'s version and re-apply only this story's own additions and removals. "
        f"Do not change any other behaviour. "
        f"Run only the tests for the files you touch plus the guard tests; commit; push with "
        f"--force-with-lease to HEAD:refs/heads/{branch}."
    )


def _git(repo_root, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _merge_tree_paths(repo_root, base_ref: str, branch_ref: str) -> list[str]:
    """Paths that conflict if ``branch_ref`` is merged into ``base_ref``.

    ``git merge-tree --write-tree`` exits 0 for a clean merge and 1 for a
    conflicted one; its conflicted-file section lists ``<mode> <oid> <stage>\\t<path>``
    after the tree id. Any other exit code raises, so the caller fails closed.
    """
    result = _git(repo_root, "merge-tree", "--write-tree", "--no-messages", base_ref, branch_ref)
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        raise RuntimeError(f"merge-tree exited {result.returncode}")
    paths: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        _info, separator, path = line.partition(_CONFLICT_INFO_SEPARATOR)
        if separator and path and path not in paths:
            paths.append(path)
    if not paths:
        raise RuntimeError("merge-tree reported a conflict with no paths")
    return sorted(paths)


def _observe(repo_root, base: str, branch: str) -> dict:
    """Live git evidence: the base SHA, the conflicting paths, and commits ahead.

    Fetches both refs from ``origin`` first. Raises on any failed git call.
    """
    fetched = _git(repo_root, "fetch", "--quiet", "origin", base, branch)
    if fetched.returncode != 0:
        raise RuntimeError("fetch failed")
    base_ref = f"origin/{base}"
    branch_ref = f"origin/{branch}"
    sha = _git(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    ahead = _git(repo_root, "rev-list", "--count", f"{base_ref}..{branch_ref}")
    if sha.returncode != 0 or ahead.returncode != 0:
        raise RuntimeError("rev-parse or rev-list failed")
    return {
        "base_sha": sha.stdout.strip(),
        "paths": _merge_tree_paths(repo_root, base_ref, branch_ref),
        "ahead": int(ahead.stdout.strip()),
    }


def _repo_root():
    from .server import REPO_ROOT

    return REPO_ROOT


def _default_branch() -> str:
    from .server import _default_branch as default_branch

    return default_branch()


def _attempts(story: dict) -> list | None:
    """The story's attempt log, or ``None`` when it is present but unreadable."""
    value = story.get("rebase_attempts", [])
    if not isinstance(value, list):
        return None
    if not all(isinstance(entry, dict) and isinstance(entry.get("base_sha"), str) for entry in value):
        return None
    return value


def _refusal(story: dict, live: dict, attempts: list) -> str | None:
    """Why the preconditions do not allow a worker, or ``None`` when they do."""
    paths = live["paths"]
    denied = [path for path in paths if is_denied_path(path)]
    if denied:
        return f"conflict in a protected path ({', '.join(denied)})"
    declared = story.get("files") if isinstance(story.get("files"), list) else []
    outside = [path for path in paths if path not in declared and not _is_test_path(path)]
    if outside:
        return f"conflict outside the story's files ({', '.join(outside)})"
    if any(entry["base_sha"] == live["base_sha"] for entry in attempts):
        return f"already attempted on base {live['base_sha'][:12]}"
    if len(attempts) >= RESOLVE_REBASE_ATTEMPT_CAP:
        return f"attempt cap of {RESOLVE_REBASE_ATTEMPT_CAP} reached"
    if live["ahead"] > RESOLVE_REBASE_MAX_COMMITS_AHEAD:
        return f"{live['ahead']} commits ahead of the base (limit {RESOLVE_REBASE_MAX_COMMITS_AHEAD})"
    return None


def _notify(plan_name, story_key, story, message: str, event: str) -> None:
    _notify_user(
        plan_name,
        message,
        story_key=story_key,
        event=event,
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )


def execute_resolve_rebase(plan_name, story_key, story, ruling, manifest) -> str:
    """Resolve a ``rebase_conflict`` park by unparking or by dispatching a worker.

    Shadow mode returns ``"shadow"`` without reading git or touching the story.
    The live check runs before any mutation. Each attempt is recorded in
    ``story["rebase_attempts"]`` as ``{base_sha, ts}`` before the story changes,
    and the ``rebase_attempts`` log is what the attempt caps read.
    """
    if _triage_mode() != "execute":
        return "shadow"
    park = classify_park(story, {})
    if not check_ruling_allowed(park, "resolve_rebase"):
        return _park(
            plan_name, story_key, story,
            f"resolve_rebase refused for park class {park.name}; parked for a human",
        )
    branch = story.get("branch")
    if not isinstance(branch, str) or not branch:
        return _park(
            plan_name, story_key, story,
            "resolve_rebase declined: no branch is recorded for this story; parked for a human",
        )
    attempts = _attempts(story)
    if attempts is None:
        return _park(
            plan_name, story_key, story,
            "resolve_rebase declined: rebase_attempts is unreadable; parked for a human",
        )
    try:
        base = _default_branch()
        live = _observe(_repo_root(), base, branch)
    except Exception:  # noqa: BLE001 - fail closed on any git or config failure
        return _park(
            plan_name, story_key, story,
            "resolve_rebase declined: live git evidence could not be read; parked for a human",
        )
    stamp = datetime.now(UTC).isoformat()
    if not live["paths"]:
        story["rebase_attempts"] = [*attempts, {"base_sha": live["base_sha"], "ts": stamp}]
        try:
            clear_park_for_unpark(plan_name, story_key, story)
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            return _park(
                plan_name, story_key, story,
                f"resolve_rebase failed to unpark ({type(exc).__name__}); parked for a human",
            )
        _notify(plan_name, story_key, story,
                f"{story_key} unparked by overlord: rebase conflict gone on live evidence",
                "story_unparked_by_overlord")
        return "resolve_rebase"
    refusal = _refusal(story, live, attempts)
    if refusal is not None:
        return _park(plan_name, story_key, story, f"resolve_rebase declined: {refusal}; parked for a human")
    story["rebase_attempts"] = [*attempts, {"base_sha": live["base_sha"], "ts": stamp}]
    task = build_rebase_task(base, live["paths"], branch)
    history = story.get("triage_diagnosis")
    history = list(history) if isinstance(history, list) else []
    history.append({"ts": stamp, "class": park.name, "text": task})
    story["triage_diagnosis"] = history[-DIAGNOSIS_HISTORY:]
    story.pop("parked_reason", None)
    story["status"] = "todo"
    _notify(plan_name, story_key, story,
            f"{story_key} rebase conflict handed to a worker: {', '.join(live['paths'])}",
            "story_rebase_dispatched_by_overlord")
    return "resolve_rebase"
