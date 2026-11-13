"""Guardrail and executor for the triage ``amend_scope`` action.

The overlord's ruling is advisory: :func:`check_scope_amendment` is the
deterministic gate that decides whether a recorded scope conflict may widen a
story's ``files`` list. Anything not explicitly allowed is refused.
"""

import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .module_ref import _ModuleRef
from .persistence import _notify_user
from .plan_approval import (
    APPROVAL_DIGEST_VERSION,
    approval_enabled,
    canonical_digest,
    digest_version_is_current,
    reset_if_changed,
)
from .scope_gate import is_test_path
from .triage_park_classes import classify_park

_park = _ModuleRef("pipeline.triage", "_park")

MAX_SCOPE_AMENDMENTS = 3
# Evidence from the scope gate or a failing guard is a weaker signal than a
# recorded conflict, so it gets its own caps: a per-amendment path cap and a
# per-story amendment-event cap.
MAX_PATHS_PER_AMENDMENT = 3
MAX_EVIDENCE_AMENDMENTS = 2

SCOPE_CONFLICT = "scope_conflict"
SCOPE_GATE = "scope_gate"
GUARD_DEMANDS_PATH = "guard_demands_path"
EVIDENCE_CLASSES = frozenset({SCOPE_GATE, GUARD_DEMANDS_PATH})

# Safety-gate modules: a reported conflict must never widen the gate that polices it.
PROTECTED_MODULES = frozenset(
    {
        "pipeline/scope_gate.py",
        "pipeline/scope_conflict.py",
        "pipeline/no_progress.py",
        "pipeline/branch_guard.py",
        "pipeline/triage.py",
        "pipeline/triage_scope_actions.py",
        "pipeline/overlord.py",
        "pipeline/review_orchestrator.py",
        "pipeline/merge.py",
        "pipeline/advance_merge.py",
        "pipeline/dispatch_lease.py",
        "pipeline/pin_guard.py",
        "pipeline/security_audit.py",
    }
)

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
    return (
        not path
        or path.startswith("/")
        or "\\" in path
        or ".." in path.split("/")
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    )


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


_DENIED_NAMES = frozenset({"overlord-authority.json"})
_DENIED_COMPONENTS = frozenset({"security", "secrets", "keys"})
_LOCKFILE_SUFFIXES = (".lock", "-lock.json", "-lock.yaml", "-lock.yml")
_KEY_SUFFIXES = (".pem", ".key")


def _is_denied(path: str) -> bool:
    """Paths no evidence class may add: authority file, key material, lockfiles."""
    pure = PurePosixPath(path)
    name = pure.name
    return (
        name in _DENIED_NAMES
        or pure.stem.lower() in _DENIED_COMPONENTS
        or any(part.lower() in _DENIED_COMPONENTS for part in pure.parent.parts)
        or name.endswith(_LOCKFILE_SUFFIXES)
        or name.endswith(_KEY_SUFFIXES)
    )


def _is_named(path: str, text: str) -> bool:
    """True iff ``path`` appears in ``text`` as a whole path, not a prefix of a longer name."""
    return re.search(rf"(?<![\w./-]){re.escape(path)}(?![\w/-]|\.\w)", text) is not None


def _amendment_count(story: dict) -> int:
    """Number of amendment events: entries sharing one ``at`` came from one amendment."""
    entries = story.get("scope_amendments") or []
    return len({e.get("at") for e in entries if isinstance(e, dict)})


def _directory(path: str) -> str:
    return str(PurePosixPath(path).parent)


def check_scope_amendment(
    story: dict,
    paths: list[str],
    evidence: str = SCOPE_CONFLICT,
    named_text: str = "",
) -> tuple[list[str], dict[str, str]]:
    """Return ``(allowed, refusals)``; each refusal maps a path to a fixed token.

    ``evidence`` names the park class the paths came from. ``named_text`` is the
    brief plus the failing guard output: for the evidence classes in
    ``EVIDENCE_CLASSES`` every path must appear in it, and the per-amendment
    and per-story caps apply instead of the cumulative path cap.
    """
    allowed: list[str] = []
    refusals: dict[str, str] = {}
    evidence_based = evidence in EVIDENCE_CLASSES
    if evidence_based and len(paths) > MAX_PATHS_PER_AMENDMENT:
        return allowed, {str(path): "too_many_paths" for path in paths}
    declared = [f for f in story.get("files") or [] if isinstance(f, str)]
    declared_dirs = {_directory(f) for f in declared}
    prior = len(story.get("scope_amendments") or [])
    events_reached = evidence_based and _amendment_count(story) >= MAX_EVIDENCE_AMENDMENTS
    for path in paths:
        if not isinstance(path, str) or _is_malformed(path):
            refusals[str(path)] = "malformed_path"
        elif story.get("risk") == "high":
            refusals[path] = "high_risk_story"
        elif _directory(path) == ".":
            refusals[path] = "root_level_path"
        elif path in PROTECTED_MODULES:
            refusals[path] = "protected_module"
        elif path in declared or path in allowed:
            refusals[path] = "already_declared"
        elif _is_sensitive(path):
            refusals[path] = "sensitive_path"
        elif _directory(path) not in declared_dirs:
            refusals[path] = "outside_declared_packages"
        elif _is_denied(path):
            refusals[path] = "denied_path"
        elif evidence_based and is_test_path(path):
            refusals[path] = "test_path"
        elif evidence_based and not _is_named(path, named_text):
            refusals[path] = "unnamed_path"
        elif events_reached if evidence_based else prior + len(allowed) + 1 > MAX_SCOPE_AMENDMENTS:
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


def _guard_output(story: dict) -> str:
    check = story.get("last_test_check")
    output = check.get("stdout_tail") if isinstance(check, dict) else None
    return output if isinstance(output, str) else ""


def _evidence_for(story: dict) -> tuple[str, list] | None:
    """Return ``(evidence class, paths)`` for the amendment, or ``None`` if none is recorded.

    A recorded ``scope_conflict`` wins. Otherwise the park class is re-derived
    from the story's live review feedback and failing-guard output; the
    ``parked_reason`` text is never read.
    """
    conflict = story.get("scope_conflict")
    if conflict:
        return SCOPE_CONFLICT, list(conflict.get("paths") or [])
    park = classify_park(story, {"test_output": _guard_output(story)})
    if park.name in EVIDENCE_CLASSES:
        return park.name, list(park.facts.get("paths") or [])
    return None


def _named_text(story: dict) -> str:
    brief = story.get("agent_instructions")
    return f"{brief if isinstance(brief, str) else ''}\n{_guard_output(story)}"


def _is_approved_at(manifest: dict, digest: str) -> bool:
    approval = manifest.get("approval")
    return (
        isinstance(approval, dict)
        and approval.get("status") == "approved"
        and approval.get("digest") == digest
        and digest_version_is_current(approval)
    )


def _settle_approval(manifest: dict, was_approved: bool, additions_only: bool) -> None:
    """Keep an approval only across an additions-only amendment; otherwise reset it.

    The restamp rebinds the existing record to the new digest and keeps its
    status and decision metadata. Anything else goes through the same reset a
    content edit gets. Both are no-ops while the approval gate is disabled.
    """
    if was_approved and additions_only and approval_enabled():
        approval = manifest["approval"]
        approval["digest"] = canonical_digest(manifest)
        approval["digest_version"] = APPROVAL_DIGEST_VERSION
        return
    reset_if_changed(manifest)


def _execute_amend_scope(plan_name, story_key, story, ruling, manifest) -> str:
    """Apply an ``amend_scope`` ruling if, and only if, every path passes the guardrail."""
    found = _evidence_for(story)
    if found is None:
        return _park(
            plan_name, story_key, story, "amend_scope ruled but no scope conflict is recorded"
        )
    evidence, paths = found
    allowed, refusals = check_scope_amendment(
        story, paths, evidence=evidence, named_text=_named_text(story)
    )
    if refusals or not allowed:
        tokens = ", ".join(sorted(set(refusals.values()))) or "no_paths"
        return _park(
            plan_name,
            story_key,
            story,
            f"amend_scope refused by guardrail ({tokens}); parked for a human",
        )

    rationale = str(ruling.get("rationale", ""))[:300]
    reasons = (story.get("scope_conflict") or {}).get("reasons") or {}
    now = datetime.now(UTC).isoformat()
    green = _is_green_at_head(story)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if isinstance(stories, dict) and stories.get(story_key) is not story:
        stories[story_key] = story

    digest_before = canonical_digest(manifest)
    was_approved = _is_approved_at(manifest, digest_before)
    files_before = list(story.get("files") or [])
    entries = [
        {
            "path": p,
            "reason": reasons.get(p, ""),
            "ruling_rationale": rationale,
            "at": now,
            "evidence": evidence,
            "digest_before": digest_before,
        }
        for p in allowed
    ]
    story["files"] = files_before + allowed
    story["scope_amendments"] = list(story.get("scope_amendments") or []) + entries
    for key in (*_REVIEW_STATE_KEYS, "scope_conflict", "parked_reason"):
        story.pop(key, None)
    story["status"] = "tests_passed" if green else "todo"

    _settle_approval(
        manifest,
        was_approved=was_approved,
        additions_only=story["files"][: len(files_before)] == files_before,
    )
    digest_after = canonical_digest(manifest)
    for entry in entries:
        entry["digest_after"] = digest_after

    # Write through the record store at the same position: the caller
    # (run_triage_sweep, under the advance tick's plan lock) owns the lock.
    from .server import _store

    _store.records().put_manifest("default", plan_name, manifest)

    cid = story.get("correlation_id")
    try:
        _notify_user(
            plan_name,
            f"{story_key} scope amended ({evidence}): {', '.join(allowed)}",
            event="scope_amended",
            story_key=story_key,
            **({"correlation_id": cid} if cid else {}),
        )
    except Exception as exc:  # noqa: BLE001 - notification failure must not undo the amendment
        logging.getLogger("pipeline").warning(
            "scope_amended notification failed",
            extra={"story_key": story_key, "error_type": type(exc).__name__},
        )
    # The triage dispatcher records the ruling (with the real autonomy mode) and
    # pops this into the record's extras.
    story["_amended_paths"] = list(allowed)
    return "amend_scope"
