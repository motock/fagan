"""Overlord executors for ``retry_transient`` and ``rebrief`` (ov-2).

``retry_transient`` clears a ``transient_bookkeeping`` park, and only after
live git and forge evidence confirms the story's branch is on origin and its
PR is OPEN. ``rebrief`` stores the overlord's ``DIAGNOSIS:`` line in
``story["triage_diagnosis"]`` and sends the story back to ``todo``.

Both executors fail closed: a park class that does not allow the action, an
absent or unusable diagnosis, a failed live check, or a spent retry budget all
end in ``park_for_human``. Neither reads ``parked_reason`` text as evidence.

``triage_diagnosis`` is runtime state. ``canonical_digest`` hashes authored
fields only, so storing a diagnosis never changes plan approval, and
``agent_instructions`` and ``files`` are never written here.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from .branch_guard import remote_branch_exists
from .forge import get_forge
from .module_ref import _ModuleRef
from .persistence import _notify_user
from .triage_park_classes import check_ruling_allowed, classify_park
from .unpark import clear_story_fields

_park = _ModuleRef("pipeline.triage", "_park")

#: Longest diagnosis kept; a longer one is cut here and the notice says so.
DIAGNOSIS_MAX_CHARS = 600
#: How many diagnoses a story keeps, newest last.
DIAGNOSIS_HISTORY = 3
#: Transient unparks a story may receive before a human must take over.
RETRY_TRANSIENT_CAP = 2
#: The retry counters a rebrief clears. Each is also in the unpark allowlist,
#: so a rebrief never clears anything a human could not clear by hand.
REBRIEF_RESET_FIELDS = frozenset(
    {
        "ci_rework",
        "merge_attempts",
        "review_deferred_count",
        "review_inconclusive_count",
        "rework_attempts",
    }
)

_DIAGNOSIS_RE = re.compile(r"^\s*DIAGNOSIS\s*:\s*(.*)$", re.MULTILINE)
_GIT_TIMEOUT_SECONDS = 120


class PlanRepo(NamedTuple):
    """The repository an executor acts on, and that repository's default branch."""

    root: str
    default_branch: str


def _git_in(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _default_branch_of(root: Path) -> str:
    """origin's HEAD branch, else the checked-out branch, else ``main``.

    The same order the server's ``_default_branch`` uses, read from ``root``
    alone and cached nowhere.
    """
    symref = _git_in(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if symref.returncode == 0 and symref.stdout.strip():
        return symref.stdout.strip().split("/", 1)[-1]
    head = _git_in(root, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode == 0 and head.stdout.strip() not in ("", "HEAD"):
        return head.stdout.strip()
    return "main"


def plan_repo(manifest: dict) -> PlanRepo | str:
    """The plan's repository and default branch, or the reason it cannot be used.

    ``manifest["repo_root"]`` is the only source. The server's ``REPO_ROOT`` is
    never read and its default-branch cache is never touched. The path must be
    an absolute directory that is the root of a git repository; a subdirectory
    of one is refused. Git is run inside that directory only.
    """
    value = manifest.get("repo_root")
    if not isinstance(value, str) or not value:
        return "the plan records no usable repo_root"
    path = Path(value)
    if not path.is_absolute():
        return "the plan's repo_root is not an absolute path"
    if not path.is_dir():
        return "the plan's repo_root is not a directory"
    try:
        toplevel = _git_in(path, "rev-parse", "--show-toplevel")
        if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != path.resolve():
            return "the plan's repo_root is not the root of a git repository"
        return PlanRepo(value, _default_branch_of(path))
    except (OSError, subprocess.SubprocessError):
        return "the plan's repo_root could not be checked with git"


def _branch_on_origin(repo_root: str, branch: str) -> bool:
    """Live check: does ``origin`` still hold ``branch``? Raises on a failed query."""
    return remote_branch_exists(repo_root, branch)


def _pr_state(repo_root: str, pr_url: str) -> str:
    """Live check: the forge's view of ``pr_url`` in ``repo_root``. ``UNKNOWN`` on a failed call."""
    return get_forge(repo_root).pr_state(pr_url)


def _park_class(story: dict):
    check = story.get("last_test_check")
    return classify_park(
        story, {"test_output": check.get("stdout_tail") if isinstance(check, dict) else None}
    )


def _retry_count(story: dict) -> int:
    value = story.get("triage_transient_retries", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _live_evidence_refusal(story: dict, repo_root: str) -> str | None:
    """Return why live evidence does not support a retry, or ``None`` when it does.

    Every check is live and runs against ``repo_root``, the plan's repository.
    A failed query counts as not confirmed.
    """
    pr_url = story.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        return "no PR is recorded for this story"
    try:
        state = _pr_state(repo_root, pr_url)
    except Exception:  # noqa: BLE001 - fail closed on any forge failure
        return "the PR state could not be read"
    if state == "MERGED":
        return "the PR is MERGED"
    if state != "OPEN":
        return f"the PR is {state}, not OPEN"
    branch = story.get("branch")
    if not isinstance(branch, str) or not branch:
        return "no branch is recorded for this story"
    try:
        on_origin = _branch_on_origin(repo_root, branch)
    except Exception:  # noqa: BLE001 - fail closed on any git failure
        return "origin could not be queried for the branch"
    if not on_origin:
        return f"branch {branch} is no longer on origin"
    return None


def clear_park_for_unpark(plan_name, story_key, story) -> None:
    """Clear a story's ``parked_reason`` and set it to ``tests_passed``.

    The shared unpark step: ``retry_transient`` and ``resolve_rebase`` both use
    it once their live evidence has cleared. Raises what ``clear_story_fields``
    raises; the caller decides how to park.
    """
    clear_story_fields(plan_name, story_key, ["parked_reason"], status="tests_passed")
    story.pop("parked_reason", None)
    story["status"] = "tests_passed"


def execute_retry_transient(plan_name, story_key, story, ruling, manifest) -> str:
    """Unpark a transient bookkeeping park whose live evidence has cleared.

    The branch must be on origin and the PR OPEN. A merged PR is left for
    ``mark_done``: the reason is rewritten so the story no longer classifies
    as transient. The retry counter moves only after a successful unpark.
    """
    park = _park_class(story)
    if not check_ruling_allowed(park, "retry_transient"):
        return _park(
            plan_name, story_key, story,
            f"retry_transient refused for park class {park.name}; parked for a human",
        )
    retries = _retry_count(story)
    if retries >= RETRY_TRANSIENT_CAP:
        return _park(
            plan_name, story_key, story,
            f"retry_transient cap of {RETRY_TRANSIENT_CAP} reached; parked for a human",
        )
    repo = plan_repo(manifest)
    if isinstance(repo, str):
        return _park(
            plan_name, story_key, story,
            f"retry_transient declined: {repo}; parked for a human",
        )
    refusal = _live_evidence_refusal(story, repo.root)
    if refusal is not None:
        if refusal == "the PR is MERGED":
            return _park(
                plan_name, story_key, story,
                "retry_transient declined: the PR is MERGED, so mark_done owns this story",
            )
        return _park(
            plan_name, story_key, story,
            f"retry_transient declined: {refusal}; parked for a human",
        )
    try:
        clear_park_for_unpark(plan_name, story_key, story)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        return _park(
            plan_name, story_key, story,
            f"retry_transient failed to unpark ({type(exc).__name__}); parked for a human",
        )
    story["triage_transient_retries"] = retries + 1
    _notify_user(
        plan_name,
        f"{story_key} unparked by overlord: transient park cleared on live evidence",
        story_key=story_key,
        event="story_unparked_by_overlord",
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    return "retry_transient"


def parse_diagnosis(text: str) -> str | None:
    """The value of the first ``DIAGNOSIS:`` line, or ``None`` when absent or blank.

    Length is not checked here. ``execute_rebrief`` applies the cap.
    """
    match = _DIAGNOSIS_RE.search(text or "")
    if match is None:
        return None
    return match.group(1).strip() or None


def _clean_diagnosis(value) -> str | None:
    """Collapse whitespace to one paragraph; ``None`` when nothing usable remains."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def execute_rebrief(plan_name, story_key, story, ruling, manifest) -> str:
    """Store the overlord's diagnosis and send the story back to ``todo``.

    The diagnosis is whitespace-collapsed and cut at ``DIAGNOSIS_MAX_CHARS``.
    The history keeps the last ``DIAGNOSIS_HISTORY`` entries. Only the counters
    in ``REBRIEF_RESET_FIELDS`` are cleared.
    """
    park = _park_class(story)
    if not check_ruling_allowed(park, "rebrief"):
        return _park(
            plan_name, story_key, story,
            f"rebrief refused for park class {park.name}; parked for a human",
        )
    text = _clean_diagnosis(ruling.get("diagnosis"))
    if text is None:
        return _park(
            plan_name, story_key, story,
            "rebrief refused: no usable DIAGNOSIS line; parked for a human",
        )
    truncated = len(text) > DIAGNOSIS_MAX_CHARS
    text = text[:DIAGNOSIS_MAX_CHARS]
    prior = story.get("triage_diagnosis")
    history = list(prior) if isinstance(prior, list) else []
    history.append({"ts": datetime.now(UTC).isoformat(), "class": park.name, "text": text})
    story["triage_diagnosis"] = history[-DIAGNOSIS_HISTORY:]
    for field in REBRIEF_RESET_FIELDS:
        story.pop(field, None)
    story["status"] = "todo"
    note = " (diagnosis truncated to 600 characters)" if truncated else ""
    _notify_user(
        plan_name,
        f"{story_key} rebriefed by overlord{note}: {text}",
        story_key=story_key,
        event="story_rebriefed_by_overlord",
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )
    return "rebrief"


def diagnosis_block(story: dict) -> str:
    """The prompt block for the latest diagnosis, or ``""`` when there is none.

    The block is ``OVERLORD DIAGNOSIS (attempt N): <text>`` followed by a blank
    line, where N is the story's ``dispatch_attempts``. A malformed entry
    yields no block.
    """
    history = story.get("triage_diagnosis")
    if not isinstance(history, list) or not history:
        return ""
    latest = history[-1]
    if not isinstance(latest, dict) or not isinstance(latest.get("text"), str) or not latest["text"]:
        return ""
    return f"OVERLORD DIAGNOSIS (attempt {story.get('dispatch_attempts', 0)}): {latest['text']}\n\n"


__all__ = [
    "DIAGNOSIS_HISTORY",
    "DIAGNOSIS_MAX_CHARS",
    "REBRIEF_RESET_FIELDS",
    "RETRY_TRANSIENT_CAP",
    "clear_park_for_unpark",
    "diagnosis_block",
    "execute_rebrief",
    "execute_retry_transient",
    "parse_diagnosis",
]
