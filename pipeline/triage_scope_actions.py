"""Guardrail and executor for the triage ``amend_scope`` action.

The overlord's ruling is advisory: :func:`check_scope_amendment` is the
deterministic gate that decides whether a recorded scope conflict may widen a
story's ``files`` list. Anything not explicitly allowed is refused.
"""

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .module_ref import _ModuleRef
from .parsers import _atomic_write_json
from .persistence import _notify_user

_park = _ModuleRef("pipeline.triage", "_park")
_record_execution = _ModuleRef("pipeline.triage", "_record_execution")

MAX_SCOPE_AMENDMENTS = 3

_FORBIDDEN_PREFIXES = ("scripts/", ".github/", ".claude/", "docs/adr/")
_FORBIDDEN_NAMES = ("pyproject.toml",)
_FORBIDDEN_SUFFIXES = (".env", ".plist")

_REVIEW_STATE_KEYS = (
    "review_verdict",
    "last_reviewed_sha",
    "last_reviewed_logic_fingerprint",
    "last_review_findings",
    "review_feedback",
)


def _is_malformed(path: str) -> bool:
    return not path or path.startswith("/") or "\\" in path or ".." in path.split("/")


def _is_sensitive(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith(_FORBIDDEN_PREFIXES)
        or any(part.startswith(".") for part in path.split("/"))
        or name in _FORBIDDEN_NAMES
        or name.endswith(_FORBIDDEN_SUFFIXES)
        or (name.startswith("requirements") and name.endswith(".txt"))
        or name.startswith("Dockerfile")
    )


def _directory(path: str) -> str:
    return str(PurePosixPath(path).parent)


def check_scope_amendment(story: dict, paths: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return ``(allowed, refusals)``; each refusal maps a path to a fixed token."""
    allowed: list[str] = []
    refusals: dict[str, str] = {}
    declared = [f for f in story.get("files") or [] if isinstance(f, str)]
    declared_dirs = {_directory(f) for f in declared}
    prior = len(story.get("scope_amendments") or [])
    for path in paths:
        if not isinstance(path, str) or _is_malformed(path):
            refusals[str(path)] = "malformed_path"
        elif story.get("risk") == "high":
            refusals[path] = "high_risk_story"
        elif path in declared or path in allowed:
            refusals[path] = "already_declared"
        elif _is_sensitive(path):
            refusals[path] = "sensitive_path"
        elif _directory(path) not in declared_dirs:
            refusals[path] = "outside_declared_packages"
        elif prior + len(allowed) + 1 > MAX_SCOPE_AMENDMENTS:
            refusals[path] = "amendment_limit"
        else:
            allowed.append(path)
    return allowed, refusals


def _worktree_head(story: dict) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(story.get("worktree") or "."), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _is_green_at_head(story: dict) -> bool:
    check = story.get("last_test_check") or {}
    head = _worktree_head(story)
    return check.get("returncode") == 0 and head is not None and check.get("sha") == head


def _execute_amend_scope(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Apply an ``amend_scope`` ruling if, and only if, every path passes the guardrail."""
    conflict = story.get("scope_conflict")
    if not conflict:
        return _park(
            plan_name, story_key, story, "amend_scope ruled but no scope conflict is recorded"
        )
    paths = list(conflict.get("paths") or [])
    allowed, refusals = check_scope_amendment(story, paths)
    if refusals or not allowed:
        tokens = ", ".join(sorted(set(refusals.values()))) or "no_paths"
        return _park(
            plan_name,
            story_key,
            story,
            f"amend_scope refused by guardrail ({tokens}); parked for a human",
        )

    prior_status = story.get("status")
    prior_parked_reason = story.get("parked_reason")
    rationale = str(ruling.get("rationale", ""))[:300]
    reasons = conflict.get("reasons") or {}
    now = datetime.now(UTC).isoformat()
    green = _is_green_at_head(story)

    story["files"] = list(story.get("files") or []) + allowed
    story["scope_amendments"] = list(story.get("scope_amendments") or []) + [
        {"path": p, "reason": reasons.get(p, ""), "ruling_rationale": rationale, "at": now}
        for p in allowed
    ]
    for key in (*_REVIEW_STATE_KEYS, "scope_conflict", "parked_reason"):
        story.pop(key, None)
    story["status"] = "tests_passed" if green else "todo"

    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if isinstance(stories, dict) and stories.get(story_key) is not story:
        stories[story_key] = story
    _atomic_write_json(manifest_path, manifest)

    cid = story.get("correlation_id")
    try:
        _notify_user(
            plan_name,
            f"{story_key} scope amended: {', '.join(allowed)}",
            event="scope_amended",
            story_key=story_key,
            **({"correlation_id": cid} if cid else {}),
        )
    except Exception as exc:  # noqa: BLE001 - notification failure must not undo the amendment
        logging.getLogger("pipeline").warning(
            "scope_amended notification failed",
            extra={"story_key": story_key, "error_type": type(exc).__name__},
        )
    _record_execution(
        plan_name,
        story_key,
        ruling.get("action", "amend_scope"),
        "amend_scope",
        "gated",
        prior_status,
        prior_parked_reason,
        amended_paths=allowed,
    )
    return "amend_scope"
