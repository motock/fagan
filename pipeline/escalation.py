"""Escalation helpers for the pipeline MCP server.

_escalate_to_claude flips a failed local story to Claude and starts clean
(fresh worktree/branch/journal). _escalate_to_local_fallback_model does the
same but stays on the local backend with a different model (plan-scoped
opt-in). _escalate_review_to_claude escalates a local-review-convergence
failure to Claude without wiping the worktree (the code is often already
correct). _auto_escalation_enabled reports whether escalation is enabled -
PIPELINE_AUTO_ESCALATE if set, else PIPELINE_BACKEND_DISPATCH=="auto".

All read PLAN_DIR via lazy imports from the server (tests patch
p.<name>; circular-avoidance). _atomic_write_json comes from pipeline_parsers,
_notify_user from pipeline_persistence.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from app import role_registry

from .parsers import _atomic_write_json
from .persistence import _notify_user
from .rebrief import (
    collect_attempt_facts,
    collect_failure_evidence,
    compose_attempt_facts,
    compose_rebriefed_instructions,
    diagnose_failure,
)


def _escalation_target() -> tuple[str, str | None]:
    """Resolve the backend (and optional model) a stuck story escalates TO.

    Historically every escalation path flipped the story to Claude. With
    Claude usage capped, PIPELINE_ESCALATION_BACKEND / PIPELINE_ESCALATION_MODEL
    let an operator retarget escalation to another provider (e.g. an
    Ollama-served cloud model) so an escalated story re-dispatches/re-reviews
    on a non-Claude backend instead of failing against an unavailable Claude.
    Defaults to ("claude", None): the original behavior, where Claude dispatch
    resolves its own model and the escalation only flips the backend.

    The model, when set, is a raw driver tag (e.g. "deepseek-v4-flash:cloud"),
    not a registry friendly name - it is written straight to story["model"],
    mirroring how _escalate_to_local_fallback_model handles its fallback tag.
    """
    # REG-2: the escalation-specific override still wins when set - this
    # story only changes the BASE resolution underneath it. The base pair
    # now comes from role_registry.resolve_role("dispatch", ...), the same
    # resolver dispatch/advance use (REG-1), instead of the hardcoded
    # ("claude", None). Fail open to that pre-registry default when the
    # registry has no usable roles.dispatch entry (a fresh clone) or the
    # entry is malformed - never crash escalation.
    if os.environ.get("PIPELINE_ESCALATION_BACKEND") or os.environ.get(
        "PIPELINE_ESCALATION_MODEL"
    ):
        backend = (
            os.environ.get("PIPELINE_ESCALATION_BACKEND") or "claude"
        ).strip().lower() or "claude"
        model = (os.environ.get("PIPELINE_ESCALATION_MODEL") or "").strip() or None
        return backend, model
    try:
        resolution = role_registry.resolve_role("dispatch")
    except role_registry.RoleRegistryError:
        logging.getLogger("pipeline").warning(
            "role_registry could not resolve the dispatch role for "
            "escalation; failing open to the pre-registry default "
            "('claude', None)",
            exc_info=True,
        )
        return "claude", None
    return resolution.provider, resolution.model


def _escalation_label() -> str:
    """Display name for the escalation target in operator-facing notifications.

    Preserves the capitalized "Claude" the existing notifications and their
    tests expect when the target is the default; other backends render as
    their lowercase driver name."""
    backend, _ = _escalation_target()
    return "Claude" if backend == "claude" else backend


def _stamp_first_dispatch(story: dict) -> None:
    """Record the tier a story was FIRST dispatched on, before escalation
    overwrites it with the escalation target.

    Escalation rewrites ``backend`` / ``model`` / ``dispatched_model`` to the
    target it escalates TO, so afterwards a story that failed on the local tier
    is indistinguishable from one that ran on the escalation tier from the
    start - which is exactly the attribution the first-pass-clean metric needs.
    ``setdefault`` makes the stamp sticky: a second escalation never overwrites
    the original tier.
    """
    story.setdefault("pre_escalation_backend", story.get("backend"))
    story.setdefault(
        "pre_escalation_model",
        story.get("dispatched_model") or story.get("model"),
    )



def _escalation_repo_root(manifest: dict) -> Path:
    """Return the repo the escalation git teardown must run in.

    The plan's own repo_root is authoritative: plans share one PLAN_DIR but
    each belongs to a different repo. The process-global REPO_ROOT is only a
    fallback for manifests ingested before repo_root existed - under the
    installed scheduler it holds the /nonexistent-repo-root-set-per-plan-only
    sentinel, so it must never be the primary source.
    """
    from .server import REPO_ROOT

    return Path(manifest.get("repo_root") or REPO_ROOT)


def _escalate_to_claude(
    manifest: dict, plan_name: str, story_key: str, manifest_path: Path
) -> None:
    """Flip a failed local story to Claude and start clean.

    Tears down the local worktree+branch (the local agent left it dirty/broken;
    Claude gets a fresh branch from main so it doesn't inherit that state), clears
    the dispatch counters, and resets status to 'todo' so the next tick
    re-dispatches on Claude. The journal is also cleared: there's nothing useful
    to resume from a failed local run when Claude is starting over. Also invoked
    from check_story_status's step-cap streak path (see
    STEP_CAP_FALLBACK_THRESHOLD), not just the test-failure caller - the same
    clean-slate teardown applies since a repeated step-cap streak isn't a
    trustworthy foundation for Claude to build on either.
    """
    from .server import PLAN_DIR
    story = manifest["stories"][story_key]
    repo_root = _escalation_repo_root(manifest)
    worktree = story.get("worktree", "")
    branch = f"agent/{story_key.lower()}"
    # A rework round can leave the worktree HEAD on an alias branch
    # agent/<key>-<suffix>. Resolve it BEFORE the worktree is removed below
    # (rev-parse needs the worktree) so the teardown deletes the branch the
    # agent actually built on, not just the convention name - otherwise the
    # alias lingers after the force worktree removal.
    from .pr import _resolve_story_branch

    resolved = _resolve_story_branch(worktree, story_key)
    # CLAUDE.md Step 9: encode the diagnosis into the next attempt's
    # instructions rather than an open-ended retry. Must run BEFORE the
    # worktree is removed below - the evidence (agent.log) lives there.
    # diagnose_failure fails open (returns None) on any error, in which case
    # compose_rebriefed_instructions is a no-op and this is a plain blind
    # retry exactly as before this existed.
    facts = collect_attempt_facts(worktree, story)
    evidence = collect_failure_evidence(worktree, story, facts=facts)
    diagnosis = diagnose_failure(evidence, story)
    story["agent_instructions"] = compose_rebriefed_instructions(
        story.get("agent_instructions", ""), diagnosis)
    story["agent_instructions"] = compose_attempt_facts(
        story.get("agent_instructions", ""), facts)
    # Remove worktree and branch — best-effort (may already be gone).
    if worktree:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                        check=False, cwd=repo_root, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    check=False, cwd=repo_root, capture_output=True, text=True)
    if resolved != branch:
        # Best-effort, same error-swallowing style as the deletes above: the
        # alias may already be gone (e.g. squash-merged by _merge_pr).
        subprocess.run(["git", "branch", "-D", resolved],
                        check=False, cwd=repo_root, capture_output=True, text=True)
    # Clear journal so Claude starts fresh (not from a broken local checkpoint).
    journal_path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    if journal_path.exists():
        journal_path.unlink()
    # Reset the story: escalation-target dispatch on next tick. The target is
    # Claude by default; PIPELINE_ESCALATION_BACKEND/MODEL retarget it (e.g. to
    # a non-Claude provider while Claude usage is capped). Only set the model
    # when one is configured - the default leaves story["model"] untouched so
    # Claude dispatch resolves its own model exactly as before this existed.
    _stamp_first_dispatch(story)
    backend, model = _escalation_target()
    story["backend"] = backend
    if model:
        story["model"] = model
    story["escalated"] = True
    story["status"] = "todo"
    for key in ("pid", "worktree", "log", "dispatch_attempts", "dispatch_error",
                "step_cap_streak", "step_cap_streak_model",
                "infra_failure_streak", "infra_failure_streak_model"):
        story.pop(key, None)
    _atomic_write_json(manifest_path, manifest)


def _escalate_to_local_fallback_model(
    manifest: dict, plan_name: str, story_key: str, manifest_path: Path,
    fallback_model: str,
) -> None:
    """Flip a failed local story to a different local model and start clean.

    Plan-scoped opt-in (see manifest["local_model_fallback"]): when a plan
    designates a fallback model, a story whose primary local model failed
    gets one retry on that fallback before falling through to the terminal
    park/fail path, instead of parking immediately. Stays on the "local"
    backend throughout - unlike _escalate_to_claude, this never spends Claude;
    it exists for plans that want a second local opinion (e.g. a larger/
    different Ollama model) without escalating to Claude at all. Mirrors
    _escalate_to_claude's clean-slate teardown (fresh worktree/branch/journal)
    since the prior run may have left broken/half-written state a different
    model shouldn't inherit.
    """
    from .server import PLAN_DIR
    story = manifest["stories"][story_key]
    repo_root = _escalation_repo_root(manifest)
    worktree = story.get("worktree", "")
    branch = f"agent/{story_key.lower()}"
    # A rework round can leave the worktree HEAD on an alias branch
    # keep local backend, set fallback model
    story["backend"] = "local"
    story["model"] = fallback_model
    from .pr import _resolve_story_branch

    resolved = _resolve_story_branch(worktree, story_key)
    # CLAUDE.md Step 9: encode the diagnosis into the next attempt's
    # instructions rather than an open-ended retry. Must run BEFORE the
    # worktree is removed below - the evidence (agent.log) lives there.
    # diagnose_failure fails open (returns None) on any error, in which case
    # compose_rebriefed_instructions is a no-op and this is a plain blind
    # retry exactly as before this existed.
    facts = collect_attempt_facts(worktree, story)
    evidence = collect_failure_evidence(worktree, story, facts=facts)
    diagnosis = diagnose_failure(evidence, story)
    story["agent_instructions"] = compose_rebriefed_instructions(
        story.get("agent_instructions", ""), diagnosis)
    story["agent_instructions"] = compose_attempt_facts(
        story.get("agent_instructions", ""), facts)
    # Remove worktree and branch — best-effort (may already be gone).
    if worktree:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                        check=False, cwd=repo_root, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    check=False, cwd=repo_root, capture_output=True, text=True)
    if resolved != branch:
        # Best-effort, same error-swallowing style as the deletes above: the
        # alias may already be gone (e.g. squash-merged by _merge_pr).
        subprocess.run(["git", "branch", "-D", resolved],
                        check=False, cwd=repo_root, capture_output=True, text=True)
    # Clear journal so the fallback model starts fresh, not from a broken
    # checkpoint left by the model that just failed.
    journal_path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    if journal_path.exists():
        journal_path.unlink()
    # Reset the story: fallback-model dispatch on next tick. backend is left
    # untouched (stays "local") - only the model changes.
    story["model"] = fallback_model
    story["tried_fallback_model"] = True
    story["status"] = "todo"
    for key in ("pid", "worktree", "log", "dispatch_attempts", "dispatch_error",
                "dispatched_model"):
        story.pop(key, None)
    _atomic_write_json(manifest_path, manifest)


def _escalate_review_to_claude(story: dict[str, Any], story_key: str, plan_name: str, reason: str) -> None:
    """
    (rework budget or inconclusive-review budget exhausted), give the story
    to Claude instead of parking for a human - for both review and any
    further rework, going forward.

    Unlike _escalate_to_claude (the dispatch-failure path), this does NOT
    wipe the worktree/branch: the existing code is very often already
    correct (2026-07-03's benchmark validation showed most of these parks
    hold ground-truth-correct implementations a local reviewer just
    couldn't cleanly resolve), so Claude reviewing/reworking the SAME
    worktree in place is cheaper and more likely to succeed than discarding
    it and starting over. Sets story["backend"] = "claude" so a subsequent
    redispatch (rework case) also runs on Claude - dispatch_story's own
    priority order already honors story["backend"] first, so no dispatch
    changes are needed. Resets the local rework/inconclusive counters as a
    fresh budget for Claude; a second exhaustion after escalation (checked
    by the caller via story.get("escalated")) is terminal - there is no
    further fallback past Claude, so it must park rather than escalate
    again or loop forever."""
    backend, model = _escalation_target()
    story["backend"] = backend
    if model:
        story["model"] = model
    story["escalated"] = True
    story.pop("rework_attempts", None)
    story.pop("review_inconclusive_count", None)
    # Clearing stale review state that may cause Mode 24/28 guard to
    # re‑trip on already‑fixed findings when the same worktree is reused.
    story.pop("last_review_findings", None)
    story.pop("last_reviewed_sha", None)
    story.pop("acceptance_failed_review", None)
    story.pop("review_feedback", None)
    # W4L-04: stamp the escalation notice with the story's persisted
    # correlation_id. Older manifests predate the field - the kwarg is then
    # omitted entirely so the legacy record shape is unchanged (absent, not
    # null).
    _cid_kwargs = (
        {"correlation_id": story["correlation_id"]}
        if story.get("correlation_id")
        else {}
    )
    _notify_user(
        plan_name,
        f"{story_key} escalating to {_escalation_label()} ({reason}); "
        f"retrying the same worktree with a fresh budget.",
        story_key=story_key,
        event="escalated",
        **_cid_kwargs,
    )


def _auto_escalation_enabled() -> bool:
    """Whether escalation (dispatch-failure, step-cap-streak, and review
    exhaustion escalation to Claude / a local fallback model) is enabled.
    backend, model = _escalation_target()
    story["backend"] = backend
    if model:
        story["model"] = model
    PIPELINE_BACKEND_DISPATCH=local could not turn on escalation without also
    changing dispatch routing. PIPELINE_AUTO_ESCALATE now lets an operator set
    escalation independently; when unset (or unrecognized), behavior falls
    back to the original PIPELINE_BACKEND_DISPATCH=="auto" rule so existing
    deployments see no change.
    """
    override = os.environ.get("PIPELINE_AUTO_ESCALATE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower() == "auto"


__all__ = [
    "_auto_escalation_enabled",
    "_escalate_review_to_claude",
    "_escalate_to_claude",
    "_escalate_to_local_fallback_model",
]