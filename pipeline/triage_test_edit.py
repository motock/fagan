"""Overlord executor for ``approve_test_edit`` (ov-5): park class ``pin_removed_test_failure``.

The branch removed a name that existing tests still patch, so the failing
tests cannot pass until someone edits them. The overlord never edits a test
itself. This executor re-derives the evidence from git, runs the mechanical
judge (``pipeline.test_edit_judge``) on the branch's test edits, and checks the
``existing_test_edit`` tier in the effective authority matrix. Only a passing
verdict on the named test files at an autonomous tier authorises the edit: the
authorisation goes to the worker through the ``triage_diagnosis`` channel, and
the story returns to ``todo``. Every other outcome parks the story for a human,
with the judge's reasons in the notification.

Shadow mode returns ``"shadow"`` without reading git or touching the story. The
ruling is recorded by ``pipeline.triage._apply_ruling_for_mode``, which snapshots
the prior story state for every executed ruling.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from . import test_edit_judge
from .authority_matrix import HUMAN_TIER, FileLayerSource, tier_for
from .module_ref import _ModuleRef
from .persistence import _notify_user
from .triage_park_classes import _is_test_path, check_ruling_allowed, classify_park
from .triage_rebase import _default_branch, _git, _repo_root
from .triage_rebrief import DIAGNOSIS_HISTORY

_park = _ModuleRef("pipeline.triage", "_park")
_triage_mode = _ModuleRef("pipeline.triage", "_triage_mode")

EXISTING_TEST_EDIT_CLASS = "existing_test_edit"
AUTONOMOUS_TIERS = frozenset({"auto+notify", "auto"})
APPROVED_EVENT = "story_test_edit_approved_by_overlord"
_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")


def resolve_tier() -> str:
    """The effective ``existing_test_edit`` tier. An unreadable matrix is ``human``."""
    try:
        return tier_for(FileLayerSource().resolve(), EXISTING_TEST_EDIT_CLASS)
    except Exception:  # noqa: BLE001 - an unreadable matrix fails closed
        return HUMAN_TIER


def _live_evidence(repo_root, base: str, branch: str) -> tuple[str, str, str]:
    """``(merge_base, head, diff)`` for the branch against ``origin/<base>``, read from git.

    Fetches both refs first. Raises on any failed git call.
    """
    fetched = _git(repo_root, "fetch", "--quiet", "origin", base, branch)
    if fetched.returncode != 0:
        raise RuntimeError("fetch failed")
    base_ref = f"origin/{base}"
    head_ref = f"origin/{branch}"
    merge_base = _git(repo_root, "merge-base", base_ref, head_ref)
    head = _git(repo_root, "rev-parse", "--verify", f"{head_ref}^{{commit}}")
    if merge_base.returncode != 0 or head.returncode != 0:
        raise RuntimeError("merge-base or rev-parse failed")
    diff = _git(repo_root, "diff", merge_base.stdout.strip(), head.stdout.strip())
    if diff.returncode != 0:
        raise RuntimeError("diff failed")
    return merge_base.stdout.strip(), head.stdout.strip(), diff.stdout


def _test_output(story: dict) -> str | None:
    check = story.get("last_test_check")
    value = check.get("stdout_tail") if isinstance(check, dict) else None
    return value if isinstance(value, str) else None


def _brief(story: dict) -> str:
    value = story.get("agent_instructions")
    return value if isinstance(value, str) else ""


def _decline(plan_name, story_key, story, blockers: list[str], reasons: list[str]) -> str:
    """Park for a human, naming every blocker and the judge's reasons."""
    detail = f" [{'; '.join(reasons)}]" if reasons else ""
    return _park(
        plan_name, story_key, story,
        f"approve_test_edit declined: {'; '.join(blockers)}{detail}; parked for a human",
    )


def _notify(plan_name, story_key, story, message: str, event: str) -> None:
    _notify_user(
        plan_name,
        message,
        story_key=story_key,
        event=event,
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )


def execute_approve_test_edit(plan_name, story_key, story, ruling, manifest) -> str:
    """Authorise edits to the named test files, or park the story for a human."""
    if _triage_mode() != "execute":
        return "shadow"
    branch = story.get("branch")
    if not isinstance(branch, str) or not branch:
        return _park(
            plan_name, story_key, story,
            "approve_test_edit declined: no branch is recorded for this story; parked for a human",
        )
    repo = _repo_root()
    try:
        base_sha, head_sha, diff = _live_evidence(repo, _default_branch(), branch)
    except Exception:  # noqa: BLE001 - fail closed on any git or config failure
        return _park(
            plan_name, story_key, story,
            "approve_test_edit declined: live git evidence could not be read; parked for a human",
        )
    park = classify_park(story, {"test_output": _test_output(story), "branch_diff": diff})
    if not check_ruling_allowed(park, "approve_test_edit"):
        return _park(
            plan_name, story_key, story,
            f"approve_test_edit refused for park class {park.name}; parked for a human",
        )
    name = park.facts.get("name")
    named = park.facts.get("test_files")
    if (
        not isinstance(name, str)
        or not _NAME_RE.match(name)
        or not isinstance(named, list)
        or not named
        or not all(isinstance(path, str) and _is_test_path(path) for path in named)
    ):
        return _park(
            plan_name, story_key, story,
            "approve_test_edit declined: the pinned name or its test files are not named in the live failure; "
            "parked for a human",
        )

    verdict = test_edit_judge.judge(base_sha, head_sha, repo, name=name, brief=_brief(story))
    tier = resolve_tier()
    outside = sorted(set(verdict.test_files) - set(named))
    blockers: list[str] = []
    if verdict.outcome != test_edit_judge.PASS:
        blockers.append(f"judge verdict {verdict.outcome}")
    if outside:
        blockers.append(f"edits outside the named test files ({', '.join(outside)})")
    if tier not in AUTONOMOUS_TIERS:
        blockers.append(f"{EXISTING_TEST_EDIT_CLASS} tier is {tier}")
    if blockers:
        return _decline(plan_name, story_key, story, blockers, verdict.reasons)

    files = sorted(set(named))
    text = f"Edits to {', '.join(files)} are authorised: they pin {name}, which this story removes."
    history = story.get("triage_diagnosis")
    history = list(history) if isinstance(history, list) else []
    history.append({"ts": datetime.now(UTC).isoformat(), "class": park.name, "text": text})
    story["triage_diagnosis"] = history[-DIAGNOSIS_HISTORY:]
    story.pop("parked_reason", None)
    story["status"] = "todo"
    _notify(plan_name, story_key, story,
            f"{story_key} test edit authorised by overlord: {', '.join(files)} pin {name}",
            APPROVED_EVENT)
    return "approve_test_edit"
